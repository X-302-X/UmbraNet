"""
UmbraNet — единые профили сервисов.

Один источник правды по доменам сервисов.

Разделение:
  • UI_SERVICE_PROFILES — то, что видит главное меню: название, категория,
    иконка, bypass (dns/dpi) и runtime_domains для включения/выключения сервиса;
  • SERVICE_PROFILES — внутренние id для ядра/диагностики/генерации;
  • runtime_domains — домены, которые попадают в active hostlist при выборе
    сервиса в главном меню;
  • generation_domains — список истины для AI-проверки DPI-стратегий;
  • required_domains — ключевые домены для coverage/отчётов. Сложные
    обязательные проверки (WebSocket gateway, voice regions API) живут в
    core/dpi/ai_strategy/probes.py.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def _unique(items: list[str] | tuple[str, ...]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items or []:
        d = str(raw or "").strip().lower()
        if not d or d in seen:
            continue
        seen.add(d)
        out.append(d)
    return out


YOUTUBE_RUNTIME_DOMAINS = _unique(['youtube.com',
 'www.youtube.com',
 'youtu.be',
 'youtube-nocookie.com',
 'youtubekids.com',
 'youtubei.googleapis.com',
 'youtube.googleapis.com',
 'youtubeembeddedplayer.googleapis.com',
 'googlevideo.com',
 'wide-youtube.l.google.com',
 'youtube-ui.l.google.com',
 'yt-video-upload.l.google.com',
 'ytimg.com',
 'ytimg.l.google.com',
 'i.ytimg.com',
 's.ytimg.com',
 'yt3.ggpht.com',
 'yt4.ggpht.com',
 'yt3.googleusercontent.com',
 'jnn-pa.googleapis.com',
 'play.google.com',
 # YT Music (2026-10-06): шелл страницы ждёт эти хосты — без лечения
 # они уходили голыми («не цель»), DPI их резал, страница «не до конца
 # погружалась» (крутилка), хотя треки играли.
 'googleusercontent.com',
 'gstatic.com',
 'accounts.google.com',
 'waa-pa.clients6.google.com'])


DISCORD_RUNTIME_DOMAINS = _unique(['dis.gd',
 'discord-attachments-uploads-prd.storage.googleapis.com',
 'discord.app',
 'discord.co',
 'discord.com',
 'discord.design',
 'discord.dev',
 'discord.gift',
 'discord.gifts',
 'discord.gg',
 'discord.media',
 'discord.new',
 'discord.store',
 'discord-activities.com',
 'discordactivities.com',
 'discordapp.com',
 'discordapp.net',
 'discordcdn.com',
 'discordmerch.com',
 'discordpartygames.com',
 'discordsays.com',
 'discordsez.com',
 'discordstatus.com',
 'gateway.discord.gg',
 'cdn.discordapp.com',
 'media.discordapp.net',
 'images-ext-1.discordapp.net',
 'images-ext-2.discordapp.net',
 'stable.dl2.discordapp.net',
 'dl.discordapp.net',
 'api.discord.com',
 'status.discord.com'])


UI_SERVICE_PROFILES: dict[str, dict[str, Any]] = {
    'ChatGPT / OpenAI': {
        "category": 'AI',
        "icon": '🤖',
        "runtime_domains": ['openai.com',
 'chatgpt.com',
 'api.openai.com',
 'auth0.openai.com',
 'cdn.oaistatic.com',
 'chat.openai.com',
 'ab.chatgpt.com',
 'files.oaiusercontent.com',
 'sora.com',
 'sora.chatgpt.com',
 'o3.chatgpt.com',
 'ios.chat.openai.com'],
    },
    'Claude (Anthropic)': {
        "category": 'AI',
        "icon": '🧠',
        "runtime_domains": ['claude.ai', 'anthropic.com', 'api.anthropic.com', 'statsig.anthropic.com'],
    },
    'Google Gemini': {
        "category": 'AI',
        "icon": '✨',
        "runtime_domains": ['gemini.google.com', 'bard.google.com', 'makersuite.google.com', 'aistudio.google.com'],
    },
    'Grok (xAI)': {
        "category": 'AI',
        "icon": '🦾',
        "runtime_domains": ['grok.com', 'x.ai', 'api.x.ai'],
    },
    'Perplexity': {
        "category": 'AI',
        "icon": '🔮',
        "runtime_domains": ['perplexity.ai', 'www.perplexity.ai', 'api.perplexity.ai'],
    },
    'DeepL': {
        "category": 'AI',
        "icon": '🌐',
        "runtime_domains": ['deepl.com', 'www.deepl.com', 'api.deepl.com', 'api-free.deepl.com'],
    },
    'Midjourney': {
        "category": 'AI',
        "icon": '🎨',
        "runtime_domains": ['midjourney.com', 'www.midjourney.com', 'discord.gg'],
    },
    'DeepSeek': {
        "category": 'AI',
        "icon": '🐳',
        "runtime_domains": ['deepseek.com', 'www.deepseek.com', 'api.deepseek.com', 'chat.deepseek.com'],
    },
    'Supercell (Brawl Stars, CoC)': {
        "category": 'Игры',
        "icon": '⚔️',
        "runtime_domains": ['supercell.com',
 'brawlstars.com',
 'clashroyale.com',
 'clashofclans.com',
 'hayday.com',
 'id.supercell.com'],
    },
    'Epic Games': {
        "category": 'Игры',
        "icon": '🎯',
        "runtime_domains": ['epicgames.com',
 'store.epicgames.com',
 'launcher-public-service-prod06.ol.epicgames.com',
 'account-public-service-prod.ol.epicgames.com',
 'fortnite.com'],
    },
    'Stumble Guys': {
        "category": 'Игры',
        "icon": '🥊',
        "runtime_domains": ['stumbleguys.com', 'api.stumbleguys.com'],
    },
    'Destiny 2 (Bungie)': {
        "category": 'Игры',
        "icon": '🔱',
        "runtime_domains": ['bungie.net', 'www.bungie.net', 'api.bungie.com'],
    },
    'Steam': {
        "category": 'Игры',
        "icon": '♨️',
        "runtime_domains": ['steampowered.com',
 'steamcommunity.com',
 'store.steampowered.com',
 'api.steampowered.com',
 'steamusercontent.com',
 'steamcdn.com'],
    },
    'YouTube': {
        "category": 'Медиа',
        "icon": '▶️',
        "runtime_domains": YOUTUBE_RUNTIME_DOMAINS,
    },
    'Spotify': {
        "category": 'Медиа',
        "icon": '🎵',
        "runtime_domains": ['spotify.com',
 'open.spotify.com',
 'accounts.spotify.com',
 'api.spotify.com',
 'apresolve.spotify.com',
 'spclient.wg.spotify.com'],
    },
    'Twitch': {
        "category": 'Медиа',
        "icon": '🟣',
        "runtime_domains": ['twitch.tv', 'www.twitch.tv', 'api.twitch.tv', 'static.twitchsvc.net', 'passport.twitch.tv'],
    },
    'Discord': {
        "category": 'Медиа',
        "icon": '💬',
        "runtime_domains": DISCORD_RUNTIME_DOMAINS,
    },
    'GitHub / Copilot': {
        "category": 'Работа',
        "icon": '🐙',
        "runtime_domains": ['github.com',
 'api.github.com',
 'copilot.github.com',
 'github.githubassets.com',
 'githubcopilot.com',
 'objects.githubusercontent.com',
 'raw.githubusercontent.com',
 'avatars.githubusercontent.com'],
    },
    'JetBrains': {
        "category": 'Работа',
        "icon": '🔧',
        "runtime_domains": ['jetbrains.com',
 'www.jetbrains.com',
 'account.jetbrains.com',
 'plugins.jetbrains.com',
 'download.jetbrains.com',
 'data.services.jetbrains.com'],
    },
    'Notion': {
        "category": 'Работа',
        "icon": '📝',
        "runtime_domains": ['notion.so', 'www.notion.so', 'api.notion.com', 'notion.com'],
    },
    'Figma': {
        "category": 'Работа',
        "icon": '🖼',
        "runtime_domains": ['figma.com', 'www.figma.com', 'api.figma.com', 'static.figma.com'],
    },
    'Docker Hub': {
        "category": 'Работа',
        "icon": '🔬',
        "runtime_domains": ['docker.com',
 'hub.docker.com',
 'registry-1.docker.io',
 'auth.docker.io',
 'production.cloudflare.docker.com',
 'index.docker.io'],
    },
    'Framer': {
        "category": 'Работа',
        "icon": '⚡',
        "runtime_domains": ['framer.com', 'www.framer.com', 'framerusercontent.com'],
    },
    'Modrinth': {
        "category": 'Разное',
        "icon": '🧱',
        "runtime_domains": ['modrinth.com', 'api.modrinth.com', 'cdn.modrinth.com'],
    },
    'Cloudflare': {
        "category": 'Разное',
        "icon": '☁️',
        "runtime_domains": ['cloudflare.com', 'www.cloudflare.com', 'cloudflare.net', 'cloudflareinsights.com'],
    },
}


# Какой обход нужен сервису в главном меню.
#   dns — хватает DNS-режима (xbox-dns и т.п.): ChatGPT и большинство AI/SaaS
#   dpi — нужен DPI/WinWS (SNI/QUIC). В режиме «только DNS» не откроется:
#         YouTube, Discord, стриминг, игры.
# Поле можно переопределить в самом профиле ключом "bypass".
_DPI_UI_SERVICES = {
    "YouTube",
    "Spotify",
    "Twitch",
    "Discord",
    "Steam",
    "Epic Games",
    "Supercell (Brawl Stars, CoC)",
    "Stumble Guys",
    "Destiny 2 (Bungie)",
}

for _name, _prof in UI_SERVICE_PROFILES.items():
    _prof.setdefault("bypass", "dpi" if _name in _DPI_UI_SERVICES else "dns")


SERVICE_PROFILES: dict[str, dict[str, Any]] = {
    "youtube": {
        "label": "YouTube",
        "ui_name": "YouTube",
        "runtime_domains": YOUTUBE_RUNTIME_DOMAINS,
        "generation_domains": YOUTUBE_RUNTIME_DOMAINS,
        "required_domains": [
            "youtube.com",
            "googlevideo.com",
            "ytimg.com",
            "youtubei.googleapis.com",
        ],
        "probe_family": "youtube_media",
        "quic_required": True,
    },
    "discord": {
        "label": "Discord",
        "ui_name": "Discord",
        "runtime_domains": DISCORD_RUNTIME_DOMAINS,
        "generation_domains": DISCORD_RUNTIME_DOMAINS,
        "required_domains": [
            "discord.com",
            "discordcdn.com",
            "discordapp.com",
            "discordapp.net",
            "gateway.discord.gg",
            "cdn.discordapp.com",
            "media.discordapp.net",
            "images-ext-1.discordapp.net",
            "images-ext-2.discordapp.net",
            "discord.media",
        ],
        "probe_family": "discord_gateway_cdn_voice_readiness",
    },
    "chatgpt": {
        "label": "ChatGPT",
        "ui_name": "ChatGPT / OpenAI",
        "runtime_domains": list(UI_SERVICE_PROFILES["ChatGPT / OpenAI"]["runtime_domains"]),
        "generation_domains": [
            "chatgpt.com",
            "openai.com",
            "api.openai.com",
        ],
        "required_domains": ["chatgpt.com", "openai.com"],
    },
}


def ui_services() -> dict[str, tuple[str, str, list[str]]]:
    """Возвращает SERVICES-совместимый словарь для главного меню."""
    return {
        name: (
            str(profile.get("category", "Разное")),
            str(profile.get("icon", "🧩")),
            list(profile.get("runtime_domains", []) or []),
        )
        for name, profile in UI_SERVICE_PROFILES.items()
    }


def service_bypass(name: str) -> str:
    """Какой обход нужен сервису в главном меню: 'dns' или 'dpi'."""
    prof = UI_SERVICE_PROFILES.get(str(name or "").strip()) or {}
    mode = str(prof.get("bypass") or "dns").strip().lower()
    return mode if mode in ("dns", "dpi") else "dns"


def service_bypass_map() -> dict[str, str]:
    """Имя сервиса → 'dns' | 'dpi' (для чипов в списке главного меню)."""
    return {name: service_bypass(name) for name in UI_SERVICE_PROFILES}


def service_allowed_in_mode(name: str, app_mode: str) -> bool:
    """Можно ли включить сервис в текущем режиме окна.

    combo — всё можно. dns_only не включает DPI-сервисы, dpi_only — DNS-сервисы.
    """
    mode = str(app_mode or "").strip().lower()
    if mode == "combo":
        return True
    bypass = service_bypass(name)
    if mode == "dpi_only":
        return bypass == "dpi"
    # dns_only и всё неизвестное
    return bypass == "dns"


def preset_domains() -> set[str]:
    return {d for (_, _, domains) in ui_services().values() for d in domains}


def check_domains() -> dict[str, str]:
    return {
        svc: domains[0]
        for svc, (_, _, domains) in ui_services().items()
        if domains
    }


def services_in_category(cat: str) -> list[str]:
    return [name for name, (c, _, _) in ui_services().items() if c == cat]


def service_profile(service_id: str) -> dict[str, Any]:
    return deepcopy(SERVICE_PROFILES.get(str(service_id or "").strip().lower(), {}))


def service_ids() -> list[str]:
    return list(SERVICE_PROFILES.keys())


def service_label(service_id: str) -> str:
    prof = SERVICE_PROFILES.get(str(service_id or "").strip().lower(), {})
    return str(prof.get("label") or service_id)


def service_runtime_domains(service_id: str) -> list[str]:
    prof = SERVICE_PROFILES.get(str(service_id or "").strip().lower(), {})
    return list(prof.get("runtime_domains", []) or [])


def service_generation_domains(service_id: str) -> list[str]:
    prof = SERVICE_PROFILES.get(str(service_id or "").strip().lower(), {})
    return list(prof.get("generation_domains", prof.get("runtime_domains", [])) or [])


def service_required_domains(service_id: str) -> list[str]:
    prof = SERVICE_PROFILES.get(str(service_id or "").strip().lower(), {})
    return list(prof.get("required_domains", []) or [])


def service_probe_family(service_id: str, default: str = "generic") -> str:
    prof = SERVICE_PROFILES.get(str(service_id or "").strip().lower(), {})
    return str(prof.get("probe_family") or default)
