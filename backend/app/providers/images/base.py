"""The single internal image-provider interface.

TEXT RULE — READ FIRST (roadmap §21, hard product rule)
------------------------------------------------------
Thumbnail and scene *text* is rendered with Pillow by the packaging
pipeline (channel template, exact font/weight/stroke, 100% correct
spelling). This interface therefore has **NO text-in-image path for
production use**: :meth:`ImageProvider.generate` takes no text parameter
and adapters must be prompted for text-free plates ("no text, no
letters, negative space in the text zone").

Why the rule is hard: the best generative text models are ~90–95%
accurate, i.e. 1 in 10–20 thumbnails ships misspelled if text is baked
in unattended — and Urdu Nastaliq (this product's primary thumbnail
language) is only reliable via Pillow + Noto Nastaliq Urdu, never via a
generative model.

The ONE exception is diegetic text (a sign/book cover *inside* a scene),
exposed as a separate, clearly-marked experimental method on the FLUX
API adapter only (:meth:`~.flux_api.FluxApiAdapter.generate_diegetic_text_image`),
default OFF. It is not part of this interface.

Architecture rule: no pipeline stage ever calls a vendor directly —
everything goes through :class:`ImageProvider` (in practice the
:class:`~.router.ImageRouter`).
"""

from __future__ import annotations

import abc

from .models import ImageRequest, ImageResult


class ImageProvider(abc.ABC):
    """Generate one image from an :class:`ImageRequest`.

    Implementations return the image bytes/path PLUS a licence/provenance
    record (marked ``GENERATED``) and the cost in USD. There is
    deliberately no parameter for rendering text into the image — see the
    TEXT RULE in this module's docstring.
    """

    #: Stable provider name recorded in provenance (e.g. "comfyui-local").
    name: str = "image-provider"

    @abc.abstractmethod
    async def generate(self, req: ImageRequest) -> ImageResult:
        """Generate one text-free image for ``req``."""
        raise NotImplementedError

    async def is_available(self) -> bool:
        """Whether this provider can currently serve requests.

        Default True; the local adapter overrides with a live server
        check so the router can overflow to the API.
        """
        return True
