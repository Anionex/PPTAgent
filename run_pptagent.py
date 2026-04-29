"""Run pptagent direct: markdown file → Document → PPTX (no research).

Usage:
    uv run python run_pptagent.py --markdown data/test_pptagent/ai_history_ppt/ai_history_presentation.md
    uv run python run_pptagent.py --markdown doc.md --template xunfei --output out.pptx --num-slides 7
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    raise ValueError(f"Missing required environment variable: {name}")


async def main(args: argparse.Namespace) -> None:
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
    print(f"Parse: {parse_model}, Gen: {gen_model}")

    template_dir = Path(package_join("templates")) / args.template
    if not template_dir.exists():
        available = [p.name for p in Path(package_join("templates")).iterdir() if p.is_dir()]
        raise ValueError(f"Template '{args.template}' not found. Available: {available}")

    print(f"Template: {args.template} ({template_dir})")
    prs_config = Config(str(template_dir))
    prs = Presentation.from_file(str(template_dir / "source.pptx"), prs_config)

    labler = ImageLabler(prs, prs_config)
    image_stats_path = template_dir / "image_stats.json"
    if image_stats_path.exists():
        labler.apply_stats(json.loads(image_stats_path.read_text()))

    slide_induction = json.loads((template_dir / "slide_induction.json").read_text())

    agent = PPTAgent(language_model=gen_llm, vision_model=gen_llm)
    agent.set_reference(slide_induction=slide_induction, presentation=prs)
    print(f"Layouts: {list(agent.layouts.keys())}")

    md_path = Path(args.markdown)
    md_content = md_path.read_text(encoding="utf-8")
    image_dir = str(md_path.parent / "images")
    if not Path(image_dir).exists():
        image_dir = str(md_path.parent)
    Path(image_dir).mkdir(exist_ok=True)

    print(f"Parsing markdown ({len(md_content)} chars) ...")
    doc = await Document.from_markdown(md_content, parse_llm, parse_llm, image_dir)
    print(f"Document: {len(doc.sections)} sections")

    print(f"Generating {args.num_slides} slides ...")
    pres, history = await agent.generate_pres(
        source_doc=doc,
        num_slides=args.num_slides,
        image_dir=image_dir,
        max_at_once=args.max_at_once,
    )

    if pres is None:
        raise RuntimeError("generation failed — no slides produced")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    pres.save(str(out))
    print(f"Saved {len(pres.slides)} slides → {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--markdown", required=True, help="Path to markdown file")
    parser.add_argument("--template", default="xunfei")
    parser.add_argument("--output", default="output/ai_history.pptx")
    parser.add_argument("--num-slides", type=int, default=7)
    parser.add_argument("--max-at-once", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(main(args))
