#!/usr/bin/env python3
"""Push one pre-generated MP3 to a DLNA renderer.

This is the reference implementation for ``SPEAK_CMD``.  It is intentionally
dependency-free (standard library only) and uses UPnP ``AVTransport`` to play
a URL plus ``RenderingControl`` to set the volume.  It is marked unverified
against real hardware in the PR; the service itself only invokes the configured
command, so any equivalent helper can be used instead.

Examples::

    tools/speak.py --url http://192.168.2.130:8090/head_down_1.mp3 --volume 35
    SPEAK_CMD="python /app/tools/speak.py --class {class} --variant {variant} --url {url}"
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree
from xml.sax.saxutils import escape

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from posture.phrases import PHRASES, phrase_url  # noqa: E402

SSDP_ADDRESS = ("239.255.255.250", 1900)
MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    """Text of a *direct* child with local tag ``name``.

    Used for ``<service>`` fields (serviceType/controlURL), where a deep search
    would return another service's value.
    """
    for child in element:
        if _local_name(child.tag) == name and child.text:
            return child.text.strip()
    return None


def _descendant_text(element: ElementTree.Element, name: str) -> str | None:
    """First non-empty text of any descendant with local tag ``name``.

    ``friendlyName``/``modelName`` live inside a nested ``<device>`` block, so
    they are not direct children of the root.
    """
    for node in element.iter():
        if _local_name(node.tag) == name and node.text:
            text = node.text.strip()
            if text:
                return text
    return None


def _soap_envelope(service_type: str, action: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service_type}">'
        f"{body}"
        f"</u:{action}></s:Body></s:Envelope>"
    )


def _post_soap(
    control_url: str,
    service_type: str,
    action: str,
    body: str,
    timeout: float,
) -> None:
    payload = _soap_envelope(service_type, action, body).encode("utf-8")
    request = urllib.request.Request(
        control_url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service_type}#{action}"',
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def discover(timeout: float = 3.0) -> list[dict[str, Any]]:
    """SSDP M-SEARCH for MediaRenderers and parse their descriptions."""
    message = "\r\n".join(
        [
            "M-SEARCH * HTTP/1.1",
            f"HOST: {SSDP_ADDRESS[0]}:{SSDP_ADDRESS[1]}",
            'MAN: "ssdp:discover"',
            "MX: 2",
            f"ST: {MEDIA_RENDERER}",
            "",
            "",
        ]
    ).encode("ascii")
    locations: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(0.5)
    try:
        sock.sendto(message, SSDP_ADDRESS)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, _address = sock.recvfrom(65507)
            except TimeoutError:
                continue
            for line in data.decode("utf-8", "replace").splitlines():
                if line.lower().startswith("location:"):
                    location = line.split(":", 1)[1].strip()
                    if location not in locations:
                        locations.append(location)
    finally:
        sock.close()

    renderers: list[dict[str, Any]] = []
    for location in locations:
        try:
            with urllib.request.urlopen(location, timeout=timeout) as response:
                root = ElementTree.fromstring(response.read())
        except (OSError, ElementTree.ParseError):
            continue
        friendly_name = _descendant_text(root, "friendlyName") or location
        av_control = None
        rc_control = None
        for node in root.iter():
            if _local_name(node.tag) != "service":
                continue
            service_type = _child_text(node, "serviceType") or ""
            control = _child_text(node, "controlURL")
            if not control:
                continue
            absolute = urljoin(location, control)
            if service_type == AV_TRANSPORT:
                av_control = absolute
            elif service_type == RENDERING_CONTROL:
                rc_control = absolute
        if av_control:
            renderers.append(
                {
                    "name": friendly_name,
                    "location": location,
                    "av_control": av_control,
                    "rc_control": rc_control,
                }
            )
    return renderers


def _build_url(args: argparse.Namespace) -> str:
    if args.url:
        return args.url
    if not args.class_name:
        raise ValueError("--url or --class is required")
    if args.class_name not in PHRASES:
        raise ValueError(f"unknown class {args.class_name!r}")
    variants = PHRASES[args.class_name]
    variant = args.variant % len(variants)
    base = os.environ.get("POSTURE_MEDIA_BASE_URL", "").strip()
    if not base:
        raise ValueError("--url or POSTURE_MEDIA_BASE_URL is required")
    return phrase_url(base, variants[variant])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="")
    parser.add_argument("--class", dest="class_name", default="")
    parser.add_argument("--variant", type=int, default=0)
    parser.add_argument("--name", default=os.environ.get("DLNA_RENDERER_NAME", ""))
    parser.add_argument("--control-url", default="")
    parser.add_argument("--volume-control-url", default="")
    parser.add_argument("--volume", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--discover-timeout", type=float, default=3.0)
    # Seconds to wait after Play before sending Stop.  Some renderers (the
    # Xiaomi DLNA speaker among them) stay in PLAYING after the track ends and
    # loop the phrase forever; an explicit Stop is the only reliable reset.
    # Set 0 to disable (play-and-leave, the pre-fix behaviour).
    parser.add_argument("--stop-after", type=float, default=2.5)
    args = parser.parse_args(argv)

    try:
        media_url = _build_url(args)
    except ValueError as exc:
        print(f"speak error: {exc}", file=sys.stderr)
        return 2

    av_control = args.control_url
    rc_control = args.volume_control_url
    renderer_name = "manual"
    if not av_control:
        renderers = discover(args.discover_timeout)
        if args.name:
            renderers = [item for item in renderers if args.name.lower() in item["name"].lower()]
        if not renderers:
            print(
                "speak error: no DLNA renderer found "
                "(pass --control-url or set DLNA_RENDERER_NAME)",
                file=sys.stderr,
            )
            return 2
        selected = renderers[0]
        av_control = selected["av_control"]
        rc_control = rc_control or selected.get("rc_control")
        renderer_name = selected["name"]

    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        "<dc:title>posture-reminder</dc:title>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f'<res protocolInfo="http-get:*:audio/mpeg:*">{escape(media_url)}</res>'
        "</item></DIDL-Lite>"
    )
    try:
        _post_soap(
            av_control,
            AV_TRANSPORT,
            "SetAVTransportURI",
            "<InstanceID>0</InstanceID>"
            f"<CurrentURI>{escape(media_url)}</CurrentURI>"
            f"<CurrentURIMetaData>{escape(didl)}</CurrentURIMetaData>",
            args.timeout,
        )
        _post_soap(
            av_control,
            AV_TRANSPORT,
            "Play",
            "<InstanceID>0</InstanceID><Speed>1</Speed>",
            args.timeout,
        )
        if args.volume is not None and rc_control:
            volume = max(0, min(100, args.volume))
            _post_soap(
                rc_control,
                RENDERING_CONTROL,
                "SetVolume",
                "<InstanceID>0</InstanceID><Channel>Master</Channel>"
                f"<DesiredVolume>{volume}</DesiredVolume>",
                args.timeout,
            )
        if args.stop_after > 0:
            # Wait for the phrase to finish, then reset the renderer so it
            # does not loop.  Stop is best-effort: a failure here must not
            # turn a successful playback into a reported error.
            time.sleep(args.stop_after)
            try:
                _post_soap(
                    av_control,
                    AV_TRANSPORT,
                    "Stop",
                    "<InstanceID>0</InstanceID>",
                    args.timeout,
                )
            except (OSError, urllib.error.URLError) as exc:
                print(f"speak warning: Stop failed: {exc}", file=sys.stderr)
    except (OSError, urllib.error.URLError) as exc:
        print(f"speak error: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {"ok": True, "renderer": renderer_name, "url": media_url},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
