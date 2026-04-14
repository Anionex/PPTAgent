"""Template induction script.

Usage:
    # Single template folder (must contain source.pptx or original.pptx):
    uv run python -m pptagent.scripts.template_induct pptagent/templates/xunfei

    # Batch mode over data/*/pptx/*:
    uv run python -m pptagent.scripts.template_induct --batch

    # Model config via env vars (ModelManager defaults):
    API_BASE=https://aihubmix.com/v1 LANGUAGE_MODEL=gemini-3-flash-preview \
        uv run python -m pptagent.scripts.template_induct pptagent/templates/xunfei
"""

import argparse
import asyncio
import json
import os
from glob import glob
from os.path import join

from pptagent.induct import SlideInducter
from pptagent.model_utils import ModelManager
from pptagent.multimodal import ImageLabler
from pptagent.presentation import Presentation
from pptagent.utils import Config, ppt_to_images


async def induct_folder(pr_folder: str, model_manager: ModelManager) -> None:
    """Run full template induction on a single folder."""
    config = Config(pr_folder)

    # Determine source file: normalize original.pptx → source.pptx if present,
    # otherwise use source.pptx directly.
    original = join(pr_folder, "original.pptx")
    source = join(pr_folder, "source.pptx")
    if os.path.exists(original):
        prs = Presentation.from_file(original, config)
        prs.save(source)
        print(f"  normalized original.pptx → source.pptx")
    elif os.path.exists(source):
        prs = Presentation.from_file(source, config)
    else:
        raise FileNotFoundError(f"No original.pptx or source.pptx in {pr_folder}")

    # Slide images (from source.pptx via soffice)
    slide_images_dir = join(pr_folder, "slide_images")
    if not glob(join(slide_images_dir, "*")):
        await ppt_to_images(source, slide_images_dir)
        print(f"  slide images → {slide_images_dir}")

    # Template images (layout-only version)
    template_images_dir = join(pr_folder, "template_images")
    if not glob(join(template_images_dir, "*")):
        template_pptx = join(pr_folder, "template.pptx")
        prs.save(template_pptx, layout_only=True)
        await ppt_to_images(template_pptx, template_images_dir)
        print(f"  template images → {template_images_dir}")

    # Re-parse after save (fresh state)
    prs = Presentation.from_file(source, config)

    # Image stats (captions)
    image_stats_path = join(pr_folder, "image_stats.json")
    labler = ImageLabler(prs, config)
    if os.path.exists(image_stats_path):
        labler.apply_stats(json.loads(open(image_stats_path, encoding="utf-8").read()))
        print(f"  loaded existing image_stats.json")
    else:
        stats = await labler.caption_images_async(model_manager.vision_model)
        with open(image_stats_path, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=4, ensure_ascii=False)
        labler.apply_stats(stats)
        print(f"  image_stats.json saved ({len(stats)} images)")

    # Slide induction
    slide_induction_path = join(pr_folder, "slide_induction.json")
    if not os.path.exists(slide_induction_path):
        inducter = SlideInducter(
            prs,
            slide_images_dir,
            template_images_dir,
            config,
            model_manager.image_model,
            model_manager.language_model,
            model_manager.vision_model,
        )
        reference = await inducter.content_induct(await inducter.layout_induct())
        with open(slide_induction_path, "w", encoding="utf-8") as f:
            json.dump(reference, f, indent=4, ensure_ascii=False)
        print(f"  slide_induction.json saved ({len(reference)} layouts)")

    print(f"  {pr_folder} done")


async def main():
    parser = argparse.ArgumentParser(description="Template induction for pptagent")
    parser.add_argument(
        "folder",
        nargs="?",
        help="Template folder (must contain source.pptx or original.pptx)",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Batch mode: process all data/*/pptx/* folders",
    )
    args = parser.parse_args()

    if not args.folder and not args.batch:
        parser.error("provide a folder path or use --batch")

    model_manager = ModelManager()

    if args.batch:
        folders = glob("data/*/pptx/*")
        print(f"Batch mode: {len(folders)} folders")
        sem = asyncio.Semaphore(16)

        async def bounded(folder: str) -> None:
            async with sem:
                await induct_folder(folder, model_manager)

        async with asyncio.TaskGroup() as tg:
            for folder in folders:
                tg.create_task(bounded(folder))
    else:
        await induct_folder(args.folder, model_manager)


if __name__ == "__main__":
    asyncio.run(main())
