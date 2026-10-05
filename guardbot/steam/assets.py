"""Validation helpers for remotely loaded Steam assets."""

from yarl import URL


def validate_steam_asset_url(asset_url: str) -> str:
    """Allow only HTTPS resources served by Steam-owned CDN hosts."""
    try:
        parsed = URL(asset_url)
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректный URL Steam-ресурса") from exc

    host = (parsed.host or '').rstrip('.').lower()
    trusted_host = host.endswith('.steamstatic.com') or host == 'steamcdn-a.akamaihd.net'
    if (
        parsed.scheme != 'https'
        or not trusted_host
        or parsed.user is not None
        or parsed.password is not None
        or parsed.port not in {443, None}
    ):
        raise ValueError("Разрешены только HTTPS URL доверенных Steam CDN")
    return str(parsed)
