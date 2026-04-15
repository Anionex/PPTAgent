import tempfile
import traceback
from collections.abc import Generator
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from typing import Literal

from lxml import etree
from pptagent_pptx import Presentation as load_prs
from pptagent_pptx.enum.shapes import MSO_SHAPE_TYPE
from pptagent_pptx.shapes.base import BaseShape
from pptagent_pptx.shapes.group import GroupShape as PPTXGroupShape
from pptagent_pptx.slide import Slide as PPTXSlide

from pptagent.utils import Config, get_logger, package_join

from .shapes import (
    Background,
    Closure,
    ClosureType,
    GroupShape,
    Paragraph,
    Picture,
    ShapeElement,
    StyleArg,
)

# Type variable for ShapeElement subclasses

logger = get_logger(__name__)

# EMU per point
_EMU_PER_PT = 12700
_MIN_FONT_PT = 6
_DEFAULT_FONT_PT = 18.0


def _get_explicit_font_size_pt(run, para, tf) -> float | None:
    """Return explicit font size in points, or None if fully inherited."""
    for source in (run.font, para.font, tf.font):
        if source.size is not None:
            return source.size / _EMU_PER_PT
    return None


def _is_promotable_textbox(shape) -> bool:
    """Return True if a layout/master TEXT_BOX should be promoted to slides.

    Skip short decorative text (section numbers like "01", "ONE") that would
    pollute the text pool and distort suggested_characters during induction.
    """
    if shape.shape_type != MSO_SHAPE_TYPE.TEXT_BOX:
        return False
    if not shape.has_text_frame:
        return False
    text = shape.text_frame.text.strip()
    return len(text) > 4


def _strip_layout_textboxes(prs) -> None:
    """Remove TEXT_BOX shapes with text from layouts/masters.

    Layout/master TEXT_BOXes (section headers, watermarks) would render on
    top of slide content and cause visual overlap.  Removing them ensures
    only the slide's own shapes are visible.  Short decorative text (≤4
    chars, e.g. "01") is kept as background watermarks.
    """
    for master in prs.slide_masters:
        for shape in list(master.shapes):
            if _is_promotable_textbox(shape):
                shape._element.getparent().remove(shape._element)

        for layout in master.slide_layouts:
            for shape in list(layout.shapes):
                if _is_promotable_textbox(shape):
                    shape._element.getparent().remove(shape._element)


def _iter_text_shapes(shapes):
    """Yield all shapes with text frames, recursing into groups."""
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _iter_text_shapes(shape.shapes)
        elif shape.has_text_frame:
            yield shape


def _get_line_spacing(para) -> float:
    """Return the line spacing multiplier for a paragraph.

    Reads ``<a:lnSpc><a:spcPct>`` from the paragraph XML.  Falls back to
    1.2 (PowerPoint's default single-spacing visual height).
    """
    _NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    pPr = para._element.find("a:pPr", _NS)
    if pPr is None:
        return 1.2
    lnSpc = pPr.find("a:lnSpc", _NS)
    if lnSpc is None:
        return 1.2
    spcPct = lnSpc.find("a:spcPct", _NS)
    if spcPct is not None:
        val = spcPct.get("val")
        if val is not None:
            return int(val) / 100_000  # 200000 → 2.0
    return 1.2


def _autofit_slide_text(pptx_slide: PPTXSlide) -> None:
    """Shrink font sizes on a built slide so text fits within shape bounds.

    After processing, sets auto_size to NONE on all adjusted shapes so
    PowerPoint does not resize them when opening the file.
    """
    from pptagent_pptx.enum.text import MSO_AUTO_SIZE
    from pptagent_pptx.util import Pt

    for shape in _iter_text_shapes(pptx_slide.shapes):
        tf = shape.text_frame
        width_pt = shape.width / _EMU_PER_PT
        height_pt = shape.height / _EMU_PER_PT
        if width_pt <= 0 or height_pt <= 0:
            continue
        if not tf.text.strip():
            continue

        # Collect per-paragraph font sizes; skip shape if no explicit sizes
        para_fonts: list[float] = []
        has_explicit = False
        for para in tf.paragraphs:
            pf = None
            for run in para.runs:
                sz = _get_explicit_font_size_pt(run, para, tf)
                if sz is not None:
                    has_explicit = True
                    pf = max(pf or 0, sz)
            para_fonts.append(pf)

        if not has_explicit:
            continue

        # Use the most common explicit size as fallback for paragraphs
        # where all runs are inherited
        explicit_sizes = [s for s in para_fonts if s is not None]
        fallback = max(explicit_sizes)
        para_fonts = [s if s is not None else fallback for s in para_fonts]

        # Estimate required height using actual line spacing
        required_height = 0.0
        for para, para_font in zip(tf.paragraphs, para_fonts):
            line_spacing = _get_line_spacing(para)
            text = para.text
            if not text:
                required_height += para_font * line_spacing
                continue
            cjk = sum(
                1 for c in text
                if "\u4e00" <= c <= "\u9fff"
                or "\u3040" <= c <= "\u30ff"
                or "\uac00" <= c <= "\ud7af"
            )
            char_width = (cjk * 1.0 + (len(text) - cjk) * 0.50) * para_font
            lines = max(1, -(-int(char_width) // int(width_pt)))
            required_height += lines * para_font * line_spacing

        if required_height <= height_pt:
            # Lock shape size so PowerPoint doesn't resize it
            if tf.auto_size == MSO_AUTO_SIZE.SHAPE_TO_FIT_TEXT:
                tf.auto_size = MSO_AUTO_SIZE.NONE
            continue

        scale = max(0.5, height_pt / required_height)

        for para, para_font in zip(tf.paragraphs, para_fonts):
            for run in para.runs:
                sz = _get_explicit_font_size_pt(run, para, tf)
                original = sz if sz is not None else para_font
                run.font.size = Pt(max(_MIN_FONT_PT, original * scale))

        # Lock shape size after adjusting fonts
        if tf.auto_size == MSO_AUTO_SIZE.SHAPE_TO_FIT_TEXT:
            tf.auto_size = MSO_AUTO_SIZE.NONE


@dataclass
class SlidePage:
    """
    A class to represent a slide page in a presentation.
    """

    shapes: list[ShapeElement]
    backgrounds: list[Background]
    slide_idx: int
    real_idx: int
    slide_notes: str | None
    slide_layout_name: str | None
    slide_title: str | None
    slide_width: int
    slide_height: int

    def __post_init__(self):
        # Assign group labels to group shapes
        groups_shapes_labels = []
        for shape in self.shape_filter(GroupShape):
            for group_shape in groups_shapes_labels:
                if group_shape == shape:
                    shape.group_label = group_shape.group_label
                    continue
            groups_shapes_labels.append(shape)
            shape.group_label = f"group_{len(groups_shapes_labels)}"

    @classmethod
    def from_slide(
        cls,
        slide: PPTXSlide,
        slide_idx: int,
        real_idx: int,
        slide_width: int,
        slide_height: int,
        config: Config,
        shape_cast: dict[MSO_SHAPE_TYPE, type[ShapeElement] | None],
    ) -> "SlidePage":
        """
        Create a SlidePage from a PPTXSlide.

        Args:
            slide (PPTXSlide): The slide object.
            slide_idx (int): The index of the slide.
            real_idx (int): The real index of the slide.
            slide_width (int): The width of the slide.
            slide_height (int): The height of the slide.
            config (Config): The configuration object.
            shape_cast (dict[MSO_SHAPE_TYPE, type[ShapeElement] | None]): Mapping of shape types to their corresponding ShapeElement classes.
            Set the value to None for any MSO_SHAPE_TYPE to exclude that shape type from processing.
        Returns:
            SlidePage: The created SlidePage.
        """
        backgrounds = [Background.from_slide(slide, config)]
        shapes = []
        for i, shape in enumerate(slide.shapes):
            if not shape.visible:
                continue
            if shape_cast.get(shape.shape_type, -1) is None:
                continue
            shapes.append(
                ShapeElement.from_shape(
                    slide_idx, i, shape, config, slide_width * slide_height, shape_cast
                )
            )
        for i, s in enumerate(shapes):
            if isinstance(s, Picture) and s.area / s.slide_area > 0.95:
                backgrounds.append(shapes.pop(i))
                break

        slide_layout_name = slide.slide_layout.name if slide.slide_layout else None
        slide_title = slide.shapes.title.text if slide.shapes.title else None
        slide_notes = (
            slide.notes_slide.notes_text_frame.text
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame
            else None
        )

        return cls(
            shapes,
            backgrounds,
            slide_idx,
            real_idx,
            slide_notes,
            slide_layout_name,
            slide_title,
            slide_width,
            slide_height,
        )

    def build(self, slide: PPTXSlide) -> PPTXSlide:
        """
        Build the slide page in a slide.

        Args:
            slide (PPTXSlide): The slide to build the slide page in.

        Returns:
            PPTXSlide: The built slide.
        """
        # Remove existing placeholders
        for ph in slide.placeholders:
            ph.element.getparent().remove(ph.element)

        # Build backgrounds, shapes and apply closures
        for shape in sorted(self.backgrounds + list(self), key=lambda x: x.shape_idx):
            build_shape = shape.build(slide)
            for closure in shape.closures:
                try:
                    closure.apply(build_shape)
                except Exception as e:
                    raise ValueError(f"Failed to apply closures to slides: {e}")
        return slide

    def iter_paragraphs(self) -> Generator[Paragraph, None, None]:
        for shape in self:  # this considered the group shapes
            if not shape.text_frame.is_textframe:
                continue
            for para in shape.text_frame.paragraphs:
                if para.idx != -1:
                    yield para

    def shape_filter(
        self,
        shape_type: type[ShapeElement],
        from_groupshape: bool = True,
        return_father: bool = False,
    ) -> (
        Generator[ShapeElement, None, None]
        | Generator[tuple["SlidePage", ShapeElement], None, None]
    ):
        """
        Filter shapes in the slide by type.

        Args:
            shape_type (Type[ShapeElement]): The type of shapes to filter.
            shapes (Optional[List[ShapeElement]]): The shapes to filter.

        Yields:
            ShapeElement: The filtered shapes.
        """
        for shape in self.shapes:
            if isinstance(shape, shape_type):
                if return_father:
                    yield (self, shape)
                else:
                    yield shape
            elif from_groupshape and isinstance(shape, GroupShape):
                yield from shape.shape_filter(shape_type, return_father)

    def get_content_type(self) -> Literal["text", "image"]:
        """
        Get the content type of the slide.

        Returns:
            Literal["text", "image"]: The content type of the slide.
        """
        if len(list(self.shape_filter(Picture))) == 0:
            return "text"
        return "image"

    def to_html(self, style_args: StyleArg | None = None, **kwargs) -> str:
        """
        Represent the slide page in HTML.

        Args:
            style_args (Optional[StyleArg]): The style arguments for HTML conversion.
            **kwargs: Additional arguments.

        Returns:
            str: The HTML representation of the slide page.
        """
        if style_args is None:
            style_args = StyleArg(**kwargs)
        shapes_html = [shape.to_html(style_args) for shape in self.shapes]
        shapes_html = [html for html in shapes_html if html]
        return "".join(
            [
                "<!DOCTYPE html>\n<html>\n",
                (f"<title>{self.slide_title}</title>\n" if self.slide_title else ""),
                f'<body style="width:{self.slide_width}pt; height:{self.slide_height}pt;">\n',
                "\n".join(shapes_html),
                "</body>\n</html>\n",
            ]
        )

    def to_text(self, show_image: bool = False) -> str:
        """
        Represent the slide page in text.

        Args:
            show_image (bool): Whether to show image captions.

        Returns:
            str: The text representation of the slide page.

        Raises:
            ValueError: If an image caption is not found.
        """
        text_content = ""
        for para in self.iter_paragraphs():
            if not para.text:
                continue
            if para.bullet:
                text_content += para.bullet
            text_content += para.text + "\n"
        if show_image:
            for image in self.shape_filter(Picture):
                text_content += "\n" + "Image: " + image.caption
        return text_content

    def __iter__(self):
        """
        Iterate over all shapes in the slide page.

        Yields:
            ShapeElement: Each shape in the slide page.
        """
        for shape in self.shapes:
            if isinstance(shape, GroupShape):
                yield from shape
            else:
                yield shape

    def __len__(self) -> int:
        """
        Get the number of shapes in the slide page.

        Returns:
            int: The number of shapes.
        """
        return len(self.shapes)


@dataclass
class Presentation:
    """
    PPTAgent's representation of a presentation.
    Aiming at a more readable and editable interface.
    """

    slides: list[SlidePage]
    error_history: list[tuple[int, str]]
    slide_width: float
    slide_height: float
    source_file: str
    num_pages: int

    def __post_init__(self):
        self.prs = load_prs(self.source_file)
        self.layout_mapping = {}
        for master in self.prs.slide_masters:
            for layout in master.slide_layouts:
                name = layout.name if layout.name else f"_unnamed_{id(layout)}"
                self.layout_mapping[name] = layout
        # Map auto-assigned layout names (layout_N) from slides with empty layout names
        slide_idx = 0
        for slide in self.prs.slides:
            if slide._element.get("show", 1) == "0":
                continue
            slide_idx += 1
            if not slide.slide_layout.name:
                auto_name = f"layout_{slide_idx}"
                self.layout_mapping[auto_name] = slide.slide_layout
        self.prs.core_properties.last_modified_by = "PPTAgent"

    @classmethod
    def from_file(
        cls,
        file_path: str,
        config: Config | None = None,
        shape_cast: dict[MSO_SHAPE_TYPE, type[ShapeElement]] | None = None,
    ) -> "Presentation":
        """
        Parse a Presentation from a file.

        Args:
            file_path (str): The path to the presentation file.
            config (Config): The configuration object.
            shape_cast (dict[MSO_SHAPE_TYPE, type[ShapeElement]] | None): Optional mapping of shape types to their corresponding ShapeElement classes.
            Set the value to None for any MSO_SHAPE_TYPE to exclude that shape type from processing.
        Returns:
            Presentation: The parsed Presentation.
        """
        if config is None:
            config = Config(tempfile.mkdtemp())
        prs = load_prs(file_path)
        slide_width = prs.slide_width
        slide_height = prs.slide_height
        slides = []
        error_history = []
        slide_idx = 0
        layouts = set()
        for master in prs.slide_masters:
            for layout in master.slide_layouts:
                layouts.add(layout.name if layout.name else f"_unnamed_{id(layout)}")
        num_pages = len(prs.slides)

        if shape_cast is None:
            shape_cast = {}

        # Move TEXT_BOX shapes from layouts/masters onto slides so they
        # become visible to the parsing pipeline and editable at generation.
        _strip_layout_textboxes(prs)

        for slide in prs.slides:
            # Skip slides that won't be printed to PDF, as they are invisible
            if slide._element.get("show", 1) == "0":
                continue

            slide_idx += 1
            try:
                layout_name = slide.slide_layout.name
                if not layout_name:
                    slide.slide_layout.name = f"layout_{slide_idx}"
                    layouts.add(slide.slide_layout.name)
                elif layout_name not in layouts:
                    raise ValueError(
                        f"Slide layout {layout_name} not found"
                    )
                slides.append(
                    SlidePage.from_slide(
                        slide,
                        slide_idx - len(error_history),
                        slide_idx,
                        slide_width.pt,
                        slide_height.pt,
                        config,
                        shape_cast,
                    )
                )
            except Exception as e:
                error_history.append((slide_idx, str(e)))
                logger.error(
                    "Fail to parse slide %d of %s: %s",
                    slide_idx,
                    file_path,
                    e,
                )
                logger.error(traceback.format_exc())

        return cls(
            slides, error_history, slide_width, slide_height, file_path, num_pages
        )

    def save(self, file_path: str, layout_only: bool = False) -> None:
        """
        Save the presentation to a file.

        Args:
            file_path (str): The path to save the presentation to.
            layout_only (bool): Whether to save only the layout.
        """
        _strip_layout_textboxes(self.prs)
        self.clear_slides()
        for slide in self.slides:
            if layout_only:
                self.clear_images(slide.shapes)
            pptx_slide = self.build_slide(slide)
            _autofit_slide_text(pptx_slide)
            if layout_only:
                self.clear_text(pptx_slide.shapes)
        self.prs.save(file_path)

    def build_slide(self, slide: SlidePage) -> PPTXSlide:
        """
        Build a slide in the presentation.
        """
        return slide.build(
            self.prs.slides.add_slide(self.layout_mapping[slide.slide_layout_name])
        )

    def validate(self, slide: SlidePage) -> PPTXSlide:
        """
        Build a slide in the presentation.
        """

        from pptagent.apis import del_para

        for shape in slide:
            if not shape.text_frame.is_textframe:
                continue
            for para in shape.text_frame.paragraphs:
                if not para.edited and para.idx != -1:
                    shape._closures[ClosureType.POST_PROCESS].append(
                        Closure(
                            partial(del_para, para.real_idx),
                            para.real_idx,
                        )
                    )

        pptx_slide = self.build_slide(slide)
        _autofit_slide_text(pptx_slide)
        return pptx_slide

    def clear_slides(self):
        """
        Delete all slides from the presentation.
        """
        while len(self.prs.slides) != 0:
            rId = self.prs.slides._sldIdLst[0].rId
            self.prs.part.drop_rel(rId)
            del self.prs.slides._sldIdLst[0]

    def clear_images(self, shapes: list[ShapeElement]):
        for shape in shapes:
            if isinstance(shape, GroupShape):
                self.clear_images(shape.shapes)
            elif isinstance(shape, Picture):
                shape.img_path = package_join("resource", "pic_placeholder.png")

    def clear_text(self, shapes: list[BaseShape]):
        for shape in shapes:
            if isinstance(shape, PPTXGroupShape):
                self.clear_text(shape.shapes)
            elif shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        run.text = "a" * len(run.text)

    def to_text(self, show_image: bool = False) -> str:
        """
        Represent the presentation in text.
        """
        return "\n----\n".join(
            [
                (
                    f"Slide {slide.slide_idx} of {len(self.slides)}\n"
                    + (f"Title:{slide.slide_title}\n" if slide.slide_title else "")
                    + slide.to_text(show_image)
                )
                for slide in self.slides
            ]
        )

    def __iter__(self):
        yield from self.slides

    def __len__(self) -> int:
        """
        Get the number of slides in the presentation.
        """
        return len(self.slides)

    def __getstate__(self) -> object:
        state = self.__dict__.copy()
        state["prs"] = None
        state["layout_mapping"] = None
        return state

    def __setstate__(self, state: object):
        self.__dict__.update(state)
        self.__post_init__()
