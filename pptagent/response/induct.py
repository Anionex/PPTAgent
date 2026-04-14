from typing import Literal

from pydantic import BaseModel

from pptagent.utils import edit_distance


class SlideElement(BaseModel):
    name: str
    data: list[str]
    type: Literal["text", "image"]


class SlideSchema(BaseModel):
    elements: list[SlideElement]

    @classmethod
    def response_model(
        cls,
        text_fields: list[str],
        image_fields: list[str] | None = None,
    ) -> type[BaseModel]:
        """Return a dynamic schema class with text/image pools captured in closure.

        Previous implementation used ContextVars, which race under concurrent
        asyncio tasks (all tasks end up reading the last-set value).  Capturing
        the pools in a closure-derived subclass avoids the race entirely.
        """
        text_pool = list(text_fields)
        image_pool = list(image_fields or [])

        class BoundSlideElement(SlideElement):
            def model_post_init(self, _):
                pool = image_pool if self.type == "image" else text_pool
                if pool:
                    self.data = [
                        max(pool, key=lambda x: edit_distance(x, d))
                        for d in self.data
                    ]

        class BoundSlideSchema(cls):
            elements: list[BoundSlideElement]

        return BoundSlideSchema
