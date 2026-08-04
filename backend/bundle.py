"""Self-contained export bundle: FCPXML + every asset it references, as a ZIP.

The FCPXML the pipeline emits points at absolute container paths
(``file:///gcs/bestiary/manananggal/render/s001.mp4``). Those resolve on Cloud
Run and nowhere else, so downloading the bare XML to a workstation yields a
timeline where all 46 references are offline. The export was, in practice, not
a deliverable.

This rewrites every ``src`` to a path relative to the XML's own location and
packs the referenced media alongside it, so the ZIP opens in DaVinci Resolve
after nothing more than an unzip.

    <slug>_bundle.zip
      <slug>.fcpxml        <- src="media/render/s001.mp4"
      media/render/*.mp4
      media/audio/**/*.mp3
      media/audio_pool/<bed>
      metadata.txt         <- title/description/tags, when generated

Only files the XML actually references are included; nothing walks the project
directory blindly, so drafts, variations and manifests stay out.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from . import config
from .manifest import Storyboard, load


def _decode_src(src: str) -> Path | None:
    """Filesystem path for an FCPXML ``src`` attribute, or None if not local."""
    if not src:
        return None
    if src.startswith("file://"):
        p = urllib.parse.unquote(urllib.parse.urlparse(src).path)
        # file:///C:/x on Windows parses to "/C:/x"
        if re.match(r"^/[A-Za-z]:", p):
            p = p[1:]
        return Path(p)
    if src.startswith(("http://", "https://")):
        return None
    return Path(urllib.parse.unquote(src))


def _arcname(p: Path) -> str:
    """Where a source file lands inside the bundle.

    Keyed off the directory the file lives in rather than its absolute path, so
    the layout is identical whether the bundle was built on Cloud Run (/gcs/...)
    or locally.
    """
    parts = [x.lower() for x in p.parts]
    for marker in ("render", "narration", "sfx", "audio_pool", "assets"):
        if marker in parts:
            i = len(parts) - 1 - parts[::-1].index(marker)
            return "media/" + "/".join(p.parts[i:])
    return f"media/{p.name}"


def build(storyboard: Storyboard | None = None, log=print) -> Path:
    """Write ``<slug>_bundle.zip`` beside the manifest and return its path."""
    sb = storyboard or load()
    ep = config.episode_paths(sb.title)
    slug = ep["slug"]
    proj_dir = Path(config.MANIFEST_PATH).parent
    xml_path = proj_dir / f"{slug}.fcpxml"
    if not xml_path.is_file():
        raise FileNotFoundError(
            f"No {slug}.fcpxml yet — run the timeline stage before bundling."
        )

    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Rewrite every src to a bundle-relative path, collecting what to pack.
    to_pack: dict[str, Path] = {}
    missing: list[str] = []
    rewritten = 0
    for el in root.iter():
        src = el.get("src")
        if not src:
            continue
        p = _decode_src(src)
        if p is None:
            continue
        if not p.is_file():
            missing.append(str(p))
            continue
        arc = _arcname(p)
        to_pack[arc] = p
        # Relative, not absolute: the point is that it resolves wherever the
        # ZIP is unpacked. Resolve accepts a plain relative src.
        el.set("src", arc)
        rewritten += 1

    if missing:
        log(f"bundle: {len(missing)} referenced file(s) not on disk and left out:")
        for m in missing[:5]:
            log(f"  !! {m}")

    out_zip = proj_dir / f"{slug}_bundle.zip"
    tmp_zip = out_zip.with_suffix(".zip.tmp")

    total = sum(p.stat().st_size for p in to_pack.values())
    log(f"bundle: {rewritten} reference(s) rewritten, packing "
        f"{len(to_pack)} file(s) / {total/1024/1024:.1f} MB ...")

    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        # Media is already compressed (H.264, MP3); level 1 keeps the CPU cost
        # down for a negligible size difference.
        z.writestr(f"{slug}.fcpxml",
                   ET.tostring(root, encoding="utf-8", xml_declaration=True))
        for arc, p in sorted(to_pack.items()):
            z.write(p, arc)

        otio = proj_dir / f"{slug}.otio"
        if otio.is_file():
            z.write(otio, f"{slug}.otio")

        from . import metadata as md_mod
        md = md_mod.load_saved(sb)
        if md:
            body = (
                f"TITLE\n{md.title}\n\n"
                f"DESCRIPTION\n{md.description_with_chapters()}\n\n"
                f"TAGS\n{', '.join(md.tags)}\n"
            )
            z.writestr("metadata.txt", body)

        z.writestr(
            "README.txt",
            f"{sb.title}\n\n"
            "Unzip anywhere, then import the .fcpxml into DaVinci Resolve.\n"
            "Media paths are relative to this folder, so keep the media/\n"
            "directory beside the .fcpxml.\n\n"
            f"{len(to_pack)} media file(s), {total/1024/1024:.1f} MB.\n"
            + (f"\nWARNING: {len(missing)} referenced file(s) were missing when this\n"
               "bundle was built and are not included:\n"
               + "\n".join(f"  {m}" for m in missing[:20]) + "\n" if missing else ""),
        )

    tmp_zip.replace(out_zip)
    log(f"bundle: wrote {out_zip.name} ({out_zip.stat().st_size/1024/1024:.1f} MB)")
    return out_zip
