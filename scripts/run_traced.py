"""Traced run of pptagent: dumps all intermediate inputs/outputs to trace_dir/.

Usage:
    uv run python scripts/run_traced.py \
        --markdown data/test_pptagent/ai_history_ppt/ai_history_short.md \
        --template xunfei \
        --output output/ai_history_traced.pptx \
        --num-slides 5 \
        --trace-dir output/trace_ai_history
"""

import argparse
import asyncio
import json
import os
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    raise ValueError(f"Missing required environment variable: {name}")


def _to_jsonable(obj):
    """Best-effort conversion of arbitrary objects to JSON-serializable form."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    if hasattr(obj, "model_dump"):
        return _to_jsonable(obj.model_dump())
    if hasattr(obj, "__dict__"):
        d = {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
        return {k: _to_jsonable(v) for k, v in d.items() if _is_simple(v)}
    return repr(obj)


def _is_simple(v):
    if v is None or isinstance(v, (str, int, float, bool, list, tuple, dict)):
        return True
    return hasattr(v, "model_dump") or hasattr(v, "__dict__")


def _dump(path: Path, obj, *, text: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if text:
        path.write_text(str(obj), encoding="utf-8")
    else:
        path.write_text(
            json.dumps(_to_jsonable(obj), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


async def main(args: argparse.Namespace) -> None:
    trace_dir = Path(args.trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    print(f"Trace dir: {trace_dir}")

    from pptagent.document import Document
    from pptagent.llms import AsyncLLM
    from pptagent.multimodal import ImageLabler
    from pptagent.pptgen import PPTAgent
    from pptagent.presentation import Presentation
    from pptagent.utils import Config, package_join

    api_base = _get_required_env("PPTAGENT_API_BASE")
    api_key = _get_required_env("PPTAGENT_API_KEY")
    parse_model = os.getenv("PPTAGENT_PARSE_MODEL", "gemini-3-flash-preview")
    gen_model = os.getenv("PPTAGENT_GEN_MODEL", "claude-sonnet-4-6")

    parse_llm = AsyncLLM(parse_model, api_base, api_key)
    gen_llm = AsyncLLM(gen_model, api_base, api_key)

    template_dir = Path(package_join("templates")) / args.template
    print(f"Template dir: {template_dir}")

    prs_config = Config(str(template_dir))
    prs = Presentation.from_file(str(template_dir / "source.pptx"), prs_config)

    labler = ImageLabler(prs, prs_config)
    image_stats_path = template_dir / "image_stats.json"
    if image_stats_path.exists():
        labler.apply_stats(json.loads(image_stats_path.read_text()))

    slide_induction = json.loads((template_dir / "slide_induction.json").read_text())

    # Snapshot template-level info
    _dump(
        trace_dir / "00_template_info.json",
        {
            "template_name": args.template,
            "source_pptx_path": str(template_dir / "source.pptx"),
            "num_template_slides": len(prs.slides),
            "layout_keys": list(slide_induction.keys()),
            "language": slide_induction.get("language"),
            "functional_keys": slide_induction.get("functional_keys"),
        },
    )
    _dump(trace_dir / "00_slide_induction_full.json", slide_induction)

    agent = PPTAgent(language_model=gen_llm, vision_model=gen_llm)
    agent.set_reference(slide_induction=slide_induction, presentation=prs)

    _dump(
        trace_dir / "01_layouts_after_set_reference.json",
        {
            "all_layouts": list(agent.layouts.keys()),
            "text_layouts": agent.text_layouts,
            "multimodal_layouts": agent.multimodal_layouts,
            "functional_layouts": agent.functional_layouts,
            "reference_language": agent.reference_lang.model_dump(),
        },
    )

    md_path = Path(args.markdown)
    md_content = md_path.read_text(encoding="utf-8")
    image_dir = str(md_path.parent / "images")
    if not Path(image_dir).exists():
        image_dir = str(md_path.parent)

    _dump(trace_dir / "02_input_markdown.md", md_content, text=True)

    print(f"Parsing markdown ({len(md_content)} chars) ...")
    doc = await Document.from_markdown(md_content, parse_llm, parse_llm, image_dir)
    print(f"  → {len(doc.sections)} sections")

    _dump(
        trace_dir / "03_document_overview.txt",
        doc.get_overview(include_summary=True, include_image=True),
        text=True,
    )
    _dump(
        trace_dir / "03_document_structure.json",
        {
            "language": doc.language.model_dump() if hasattr(doc.language, "model_dump") else str(doc.language),
            "metadata": doc.metadata,
            "num_sections": len(doc.sections),
            "sections": [
                {
                    "title": s.title,
                    "summary": getattr(s, "summary", None),
                    "subsections": [
                        {
                            "title": getattr(ss, "title", None),
                            "content": getattr(ss, "content", None),
                            "type": type(ss).__name__,
                        }
                        for ss in s.subsections
                    ] if hasattr(s, "subsections") else [],
                    "medias": [
                        {
                            "path": getattr(m, "path", None),
                            "caption": getattr(m, "caption", None),
                            "type": type(m).__name__,
                        }
                        for m in (s.medias if hasattr(s, "medias") else [])
                    ],
                }
                for s in doc.sections
            ],
        },
    )

    # Monkey-patch capture points
    slide_traces: dict[int, dict] = {}

    orig_select_layout = agent._select_layout
    orig_generate_content = agent._generate_content
    orig_edit_slide = agent._edit_slide
    orig_generate_slide = agent.generate_slide

    async def traced_select_layout(slide_idx, outline_item):
        header, content_source, images = outline_item.retrieve(slide_idx, agent.source_doc)
        slide_traces.setdefault(slide_idx, {})["retrieve"] = {
            "header": header,
            "content_source": content_source,
            "images": images,
        }
        layout, ret_header, slide_content = await orig_select_layout(slide_idx, outline_item)
        slide_traces[slide_idx]["select_layout_result"] = {
            "selected_layout_title": layout.title,
            "header_passed_to_editor": ret_header,
            "slide_content_passed_to_editor": slide_content,
        }
        return layout, ret_header, slide_content

    async def traced_generate_content(layout, slide_content, slide_description):
        result = await orig_generate_content(layout, slide_content, slide_description)
        command_list, template_id = result
        # find slide_idx — we'll store under "pending" since we don't have slide_idx here
        slide_traces.setdefault("_pending_content", []).append(
            {
                "layout_title": layout.title,
                "template_id": template_id,
                "command_list": [
                    {
                        "element": cmd[0],
                        "type": cmd[1],
                        "quantity_change": cmd[2],
                        "old_data": cmd[3],
                        "new_data": cmd[4],
                    }
                    for cmd in command_list
                ],
                "content_schema": layout.content_schema,
            }
        )
        return result

    async def traced_edit_slide(command_list, template_id):
        try:
            slide, code_executor = await orig_edit_slide(command_list, template_id)
        except Exception as e:
            slide_traces.setdefault("_pending_edit", []).append(
                {"template_id": template_id, "error": repr(e)[:500]}
            )
            raise
        slide_traces.setdefault("_pending_edit", []).append(
            {
                "template_id": template_id,
                "api_history": code_executor.api_history,
                "code_history": [str(c)[:2000] for c in code_executor.code_history],
                "command_history": [
                    [
                        {
                            "element": c[0],
                            "type": c[1],
                            "quantity_change": c[2],
                        }
                        if (c is not None and len(c) >= 3)
                        else {"raw": repr(c)[:200]}
                        for c in (cmd_batch or [])
                    ]
                    for cmd_batch in code_executor.command_history
                ],
            }
        )
        return slide, code_executor

    async def traced_generate_slide(slide_idx, outline_item, semaphore):
        slide_traces.setdefault(slide_idx, {})["outline_item"] = {
            "purpose": outline_item.purpose,
            "topic": outline_item.topic,
            "indexes": _to_jsonable(outline_item.indexes),
            "images": list(outline_item.images),
        }
        # snapshot pending stage queues to associate with this slide
        before_content = len(slide_traces.get("_pending_content", []))
        before_edit = len(slide_traces.get("_pending_edit", []))
        result = await orig_generate_slide(slide_idx, outline_item, semaphore)
        new_content = slide_traces.get("_pending_content", [])[before_content:]
        new_edit = slide_traces.get("_pending_edit", [])[before_edit:]
        if new_content:
            slide_traces[slide_idx]["generate_content"] = new_content[0]
        if new_edit:
            slide_traces[slide_idx]["edit_slide"] = new_edit[0]
        return result

    agent._select_layout = traced_select_layout
    agent._generate_content = traced_generate_content
    agent._edit_slide = traced_edit_slide
    agent.generate_slide = traced_generate_slide

    # Also capture outline
    orig_generate_outline = agent.generate_outline

    async def traced_generate_outline(num_slides, source_doc):
        outline = await orig_generate_outline(num_slides, source_doc)
        _dump(
            trace_dir / "04_outline_generated.json",
            [
                {
                    "purpose": o.purpose,
                    "topic": o.topic,
                    "indexes": _to_jsonable(o.indexes),
                    "images": list(o.images),
                }
                for o in outline
            ],
        )
        return outline

    agent.generate_outline = traced_generate_outline

    print(f"Generating {args.num_slides} slides ...")
    pres, history = await agent.generate_pres(
        source_doc=doc,
        num_slides=args.num_slides,
        image_dir=image_dir,
        max_at_once=2,
    )

    if pres is None:
        print("ERROR: generation failed")
        return

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    pres.save(str(out))
    print(f"Saved {len(pres.slides)} slides → {out}")

    # Persist slide-level traces
    slide_traces.pop("_pending_content", None)
    slide_traces.pop("_pending_edit", None)
    for slide_idx, data in sorted(slide_traces.items()):
        _dump(trace_dir / "05_slides" / f"slide_{slide_idx:02d}.json", data)

    # Final outline (with functional layouts injected)
    _dump(
        trace_dir / "06_outline_with_functional.json",
        [
            {
                "slide_idx": i,
                "purpose": o.purpose,
                "topic": o.topic,
                "indexes": _to_jsonable(o.indexes),
                "images": list(o.images),
            }
            for i, o in enumerate(agent.outline)
        ],
    )

    # Truncated agents history
    _dump(
        trace_dir / "07_agents_history.json",
        {
            "agents": {
                k: [
                    {
                        "role": getattr(turn, "role", None) if hasattr(turn, "role") else (turn.get("role") if isinstance(turn, dict) else None),
                        "content_preview": str(turn)[:1500],
                    }
                    for turn in v
                ]
                for k, v in history["agents"].items()
            }
        },
    )

    _dump(
        trace_dir / "08_run_metadata.json",
        {
            "timestamp": datetime.now().isoformat(),
            "markdown": str(md_path),
            "template": args.template,
            "num_slides_requested": args.num_slides,
            "num_slides_generated": len(pres.slides),
            "output": str(out),
            "models": {"parse": parse_model, "generate": gen_model},
        },
    )

    print(f"All traces written to {trace_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--markdown", required=True)
    parser.add_argument("--template", default="xunfei")
    parser.add_argument("--output", default="output/ai_history_traced.pptx")
    parser.add_argument("--num-slides", type=int, default=5)
    parser.add_argument("--trace-dir", default="output/trace_ai_history")
    args = parser.parse_args()
    asyncio.run(main(args))
