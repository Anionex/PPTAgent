import json
import traceback
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Literal

from deeppresenter.agents.design import Design
from deeppresenter.agents.env import AgentEnv
from deeppresenter.agents.planner import Planner
from deeppresenter.agents.research import Research
from deeppresenter.agents.subagent import SubAgent
from deeppresenter.utils.config import DeepPresenterConfig
from deeppresenter.utils.constants import WORKSPACE_BASE
from deeppresenter.utils.log import debug, error, set_logger, timer, warning
from deeppresenter.utils.typings import ChatMessage, ConvertType, InputRequest, Role
from deeppresenter.utils.webview import PlaywrightConverter, convert_html_to_pptx


class AgentLoop:
    def __init__(
        self,
        config: DeepPresenterConfig,
        session_id: str | None = None,
        workspace: Path = None,
        language: Literal["zh", "en"] = "en",
    ):
        self.config = config
        self.language = language
        if session_id is None:
            session_id = str(uuid.uuid4())[:8]
        self.workspace = workspace or WORKSPACE_BASE / session_id
        self.intermediate_output = {}
        self.agent = None
        set_logger(
            f"deeppresenter-loop-{self.workspace.stem}",
            self.workspace / ".history" / "deeppresenter-loop.log",
        )
        debug(f"Initialized AgentLoop with workspace={self.workspace}")
        debug(f"Config: {self.config.model_dump_json(indent=2)}")

    @timer("DeepPresenter Loop")
    async def run(
        self,
        request: InputRequest,
        check_llms: bool = False,
        soft_parsing: bool = True,
    ) -> AsyncGenerator[str | ChatMessage, None]:
        """Main loop for DeepPresenter generation process.
        Arguments:
            request: InputRequest object containing task details.
            check_llms: Whether to check LLM availability before running.
            soft_parsing: Whether to use soft parsing on html2pptx.
        Yields:
            ChatMessage or final output path (str). Outline path stored in intermediate_output["outline"].
        """
        if not self.config.design_agent.is_multimodal and self.config.heavy_reflect:
            debug(
                "Reflective design requires a multimodal LLM in the design agent, reflection will only enable on textual state."
            )
        if check_llms:
            await self.config.validate_llms()
        request.copy_to_workspace(self.workspace)
        with open(self.workspace / ".input_request.json", "w") as f:
            json.dump(request.model_dump(), f, ensure_ascii=False, indent=2)
        async with AgentEnv(self.workspace, self.config) as agent_env:
            hello_message = f"DeepPresenter running in {self.workspace}, with {len(request.attachments)} attachments, prompt={request.instruction}"
            modes = []
            if self.config.offline_mode:
                modes.append("Offline Mode")
            self.agent_env = agent_env
            if self.config.multiagent_mode:
                self.agent_env.register_tool(
                    SubAgent.delegate(
                        self.config, agent_env, self.workspace, self.language
                    )
                )
                modes.append("Multiagent Mode")
            if modes:
                hello_message += f" [{', '.join(modes)}]"
            debug(hello_message)

            yield ChatMessage(role=Role.SYSTEM, content=hello_message)

            # ── Optional Planner phase ────────────────────────────────────
            if request.enable_planner:
                self.planner = Planner(
                    self.config,
                    agent_env,
                    self.workspace,
                    self.language,
                )
                self.agent = self.planner
                self.planner_gen = self.planner.loop(request)
                try:
                    async for msg in self.planner_gen:
                        if isinstance(msg, str):
                            self.intermediate_output["outline"] = msg
                            yield msg
                            break
                        yield msg
                except Exception as e:
                    error_message = f"Planner agent failed with error: {e}\n{traceback.format_exc()}"
                    error(error_message)
                    raise e
                finally:
                    self.planner.save_history()
                    await self.planner_gen.aclose()
                    self.save_results()

            self.research_agent = Research(
                self.config,
                agent_env,
                self.workspace,
                self.language,
            )
            self.agent = self.research_agent
            try:
                async for msg in self.research_agent.loop(
                    request, self.intermediate_output.get("outline", None)
                ):
                    if isinstance(msg, str):
                        md_file = Path(msg)
                        if not md_file.is_absolute():
                            md_file = self.workspace / md_file
                        self.intermediate_output["manuscript"] = md_file
                        msg = str(md_file)
                        break
                    yield msg
            except Exception as e:
                error_message = (
                    f"Research agent failed with error: {e}\n{traceback.format_exc()}"
                )
                error(error_message)
                raise e
            finally:
                self.research_agent.save_history()
                self.save_results()

            if request.convert_type == ConvertType.PPTAGENT:
                try:
                    yield ChatMessage(
                        role=Role.SYSTEM,
                        content=f"Starting PPTAgent template generation (template={request.template})",
                    )
                    pptx_file = await self._run_pptagent(md_file, request)
                    self.intermediate_output["pptx"] = pptx_file
                    self.intermediate_output["final"] = pptx_file
                    msg = pptx_file
                except Exception as e:
                    error(f"PPTAgent failed: {e}\n{traceback.format_exc()}")
                    raise
                finally:
                    self.save_results()
            else:
                self.designagent = Design(
                    self.config,
                    agent_env,
                    self.workspace,
                    self.language,
                )
                self.agent = self.designagent
                try:
                    async for msg in self.designagent.loop(request, md_file):
                        if isinstance(msg, str):
                            slide_html_dir = Path(msg)
                            if not slide_html_dir.is_absolute():
                                slide_html_dir = self.workspace / slide_html_dir
                            self.intermediate_output["slide_html_dir"] = slide_html_dir
                            break
                        yield msg
                except Exception as e:
                    error_message = (
                        f"Design agent failed with error: {e}\n{traceback.format_exc()}"
                    )
                    error(error_message)
                    raise e
                finally:
                    self.designagent.save_history()
                    self.save_results()
                pptx_path = self.workspace / f"{md_file.stem}.pptx"
                try:
                    # ? this feature is in experimental stage
                    await convert_html_to_pptx(
                        slide_html_dir,
                        pptx_path,
                        aspect_ratio=request.powerpoint_type,
                        soft_parsing=soft_parsing,
                    )
                except Exception as e:
                    warning(
                        f"html2pptx conversion failed, falling back to pdf conversion\n{e}"
                    )
                    pptx_path = pptx_path.with_suffix(".pdf")
                    (self.workspace / ".html2pptx-error.txt").write_text(
                        str(e) + "\n" + traceback.format_exc()
                    )
                finally:
                    async with PlaywrightConverter() as pc:
                        await pc.convert_to_pdf(
                            list(slide_html_dir.glob("*.html")),
                            pptx_path.with_suffix(".pdf"),
                            aspect_ratio=request.powerpoint_type,
                        )

                self.intermediate_output["final"] = str(pptx_path)
                msg = pptx_path
            self.save_results()
            debug(f"DeepPresenter finished, final output at: {msg}")
            yield msg

    async def _run_pptagent(self, md_file: Path, request: InputRequest) -> Path:
        """Run pptagent template engine directly via Python API.

        Bypasses the MCP agent loop — calls PPTAgent.generate_pres() for
        deterministic, template-based slide generation.
        """
        from pptagent.document import Document
        from pptagent.llms import AsyncLLM
        from pptagent.multimodal import ImageLabler
        from pptagent.pptgen import PPTAgent as PPTAgentCore
        from pptagent.presentation import Presentation
        from pptagent.utils import Config, package_join

        # Build AsyncLLM from deeppresenter config
        cfg = self.config.long_context_model
        llm = AsyncLLM(cfg.model, cfg.base_url, cfg.api_key)
        debug(f"PPTAgent using model: {cfg.model}")

        # Resolve template
        template_name = request.template or "default"
        template_dir = Path(package_join("templates")) / template_name
        available = [p.name for p in Path(package_join("templates")).iterdir() if p.is_dir()]
        if not template_dir.exists():
            raise ValueError(
                f"Template '{template_name}' not found. Available: {available}"
            )
        debug(f"PPTAgent template: {template_name} ({template_dir})")

        # Load template artifacts
        prs_config = Config(str(template_dir))
        prs = Presentation.from_file(str(template_dir / "source.pptx"), prs_config)
        image_labler = ImageLabler(prs, prs_config)
        image_stats_path = template_dir / "image_stats.json"
        if image_stats_path.exists():
            image_labler.apply_stats(json.loads(image_stats_path.read_text()))
        slide_induction = json.loads(
            (template_dir / "slide_induction.json").read_text()
        )

        # Create pptagent core and set reference
        agent = PPTAgentCore(language_model=llm, vision_model=llm)
        agent.set_reference(slide_induction=slide_induction, presentation=prs)

        # Parse markdown into Document
        md_content = md_file.read_text(encoding="utf-8")
        image_dir = str(md_file.parent / "images")
        doc = await Document.from_markdown(md_content, llm, llm, image_dir)

        # Generate presentation
        num_slides = int(request.num_pages) if request.num_pages else None
        pres, history = await agent.generate_pres(
            source_doc=doc,
            num_slides=num_slides,
            image_dir=image_dir,
        )
        if pres is None:
            raise RuntimeError("PPTAgent generation failed — no slides produced")

        # Save
        output_path = self.workspace / f"{md_file.stem}.pptx"
        pres.save(str(output_path))
        debug(f"PPTAgent saved {len(pres.slides)} slides to {output_path}")
        return output_path

    def save_results(self):
        with open(self.workspace / "intermediate_output.json", "w") as f:
            json.dump(
                {k: str(v) for k, v in self.intermediate_output.items()},
                f,
                ensure_ascii=False,
                indent=2,
            )
