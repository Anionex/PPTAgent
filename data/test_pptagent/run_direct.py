"""Test pptagent direct API call — skip research, use existing markdown."""
import asyncio
import json
import shutil
from pathlib import Path

from deeppresenter.utils.config import DeepPresenterConfig
from deeppresenter.utils.log import set_logger
from deeppresenter.utils.typings import ConvertType, InputRequest

WORKSPACE = Path(__file__).parent
MD_FILE = WORKSPACE / "ai_history_ppt" / "ai_history_presentation.md"
OUTPUT = Path("/mnt/d/Desktop/AI发展史_template.pptx")
CONFIG_FILE = Path.home() / ".config" / "deeppresenter" / "config.yaml"


async def main():
    set_logger("test-direct", WORKSPACE / ".history" / "test-direct.log")

    config = DeepPresenterConfig.load_from_file(str(CONFIG_FILE))

    request = InputRequest(
        instruction="生成一套关于AI发展史的PPT",
        template="xunfei",
        num_pages="7",
        convert_type=ConvertType.PPTAGENT,
    )

    # Inline the logic from AgentLoop._run_pptagent()
    from pptagent.document import Document
    from pptagent.llms import AsyncLLM
    from pptagent.multimodal import ImageLabler
    from pptagent.pptgen import PPTAgent
    from pptagent.presentation import Presentation
    from pptagent.utils import Config, package_join

    # Claude for generation (agent), Gemini for parsing (structured output)
    cfg = config.long_context_model
    llm = AsyncLLM(cfg.model, cfg.base_url, cfg.api_key)
    print(f"Agent model: {cfg.model}")

    dcfg = config.design_agent
    parse_llm = AsyncLLM(dcfg.model, dcfg.base_url, dcfg.api_key)
    print(f"Parse model: {dcfg.model}")

    template_name = request.template or "default"
    template_dir = Path(package_join("templates")) / template_name
    print(f"Template: {template_name} ({template_dir})")

    prs_config = Config(str(template_dir))
    prs = Presentation.from_file(str(template_dir / "source.pptx"), prs_config)
    image_labler = ImageLabler(prs, prs_config)
    image_stats_path = template_dir / "image_stats.json"
    if image_stats_path.exists():
        image_labler.apply_stats(json.loads(image_stats_path.read_text()))
    slide_induction = json.loads(
        (template_dir / "slide_induction.json").read_text()
    )

    agent = PPTAgent(language_model=llm, vision_model=llm)
    agent.set_reference(slide_induction=slide_induction, presentation=prs)
    print(f"Layouts: {list(agent.layouts.keys())}")

    md_content = MD_FILE.read_text(encoding="utf-8")
    image_dir = str(MD_FILE.parent / "images")
    print(f"Parsing markdown ({len(md_content)} chars) ...")
    doc = await Document.from_markdown(md_content, parse_llm, parse_llm, image_dir)
    print(f"Document parsed: {len(doc.sections)} sections")

    num_slides = int(request.num_pages) if request.num_pages else None
    print(f"Generating {num_slides} slides ...")
    pres, history = await agent.generate_pres(
        source_doc=doc,
        num_slides=num_slides,
        image_dir=image_dir,
    )

    if pres is None:
        print("ERROR: generation failed — no slides produced")
        return

    output_path = WORKSPACE / f"{MD_FILE.stem}.pptx"
    pres.save(str(output_path))
    print(f"Saved {len(pres.slides)} slides to {output_path}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(output_path, OUTPUT)
    print(f"Done! Output: {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
