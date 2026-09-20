"""Curated remote MCP catalog and per-user installation records."""

import json
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.conversation_store import UserMcpInstallation


@dataclass(frozen=True)
class CatalogMcp:
    id: str
    name: str
    description: str
    category: str
    endpoint: str
    registry_name: str
    version: str
    repository_url: str | None
    tools: tuple[str, ...]

    def public_data(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "transport": "streamable-http",
            "endpoint": self.endpoint,
            "registry_name": self.registry_name,
            "version": self.version,
            "repository_url": self.repository_url,
            "tools": list(self.tools),
        }


# Each entry was verified with initialize, tools/list and a representative
# tools/call before inclusion. Keeping this list curated avoids executing
# arbitrary packages discovered in a public registry.
CATALOG = (
    CatalogMcp(
        id="context7",
        name="Context7 开发文档",
        description="查询主流编程库和框架的最新版本文档与代码示例。",
        category="开发文档",
        endpoint="https://mcp.context7.com/mcp",
        registry_name="io.github.upstash/context7",
        version="4.1.1",
        repository_url="https://github.com/upstash/context7",
        tools=("resolve-library-id", "query-docs"),
    ),
    CatalogMcp(
        id="arxiv",
        name="arXiv 论文检索",
        description="搜索 arXiv 论文、读取元数据、分类和论文全文。",
        category="学术研究",
        endpoint="https://arxiv.caseyjhand.com/mcp",
        registry_name="io.github.cyanheads/arxiv-mcp-server",
        version="1.5.3",
        repository_url="https://github.com/cyanheads/arxiv-mcp-server",
        tools=("arxiv_search", "arxiv_get_metadata", "arxiv_read_paper", "arxiv_list_categories"),
    ),
    CatalogMcp(
        id="wikipedia",
        name="Wikipedia 知识检索",
        description="搜索多语言 Wikipedia，读取摘要、正文、章节和附近条目。",
        category="知识检索",
        endpoint="https://wikipedia.caseyjhand.com/mcp",
        registry_name="io.github.cyanheads/wikipedia-mcp-server",
        version="0.2.2",
        repository_url="https://github.com/cyanheads/wikipedia-mcp-server",
        tools=(
            "wikipedia_search_articles", "wikipedia_get_summary", "wikipedia_get_article",
            "wikipedia_get_sections", "wikipedia_search_nearby", "wikipedia_get_languages",
        ),
    ),
    CatalogMcp(
        id="exa-search",
        name="Exa 联网搜索",
        description="搜索实时网页、新闻和技术资料，并按需读取指定网页的完整内容。",
        category="联网搜索",
        endpoint="https://mcp.exa.ai/mcp",
        registry_name="io.github.exa-labs/exa-mcp-server",
        version="3.4.1",
        repository_url="https://github.com/exa-labs/exa-mcp-server",
        tools=("web_search_exa", "web_fetch_exa"),
    ),
    CatalogMcp(
        id="weather-data",
        name="天气与地震数据",
        description="查询美国天气预报、天气预警、全球地震和地理高程数据。",
        category="实时数据",
        endpoint="https://weather.datakoot.com/mcp",
        registry_name="com.datakoot/us-weather-forecast-alerts",
        version="1.0.0",
        repository_url="https://github.com/datakoot/weather-intel-mcp",
        tools=("geocode", "weather_forecast", "weather_current", "weather_alerts", "earthquakes", "elevation"),
    ),
    CatalogMcp(
        id="qt-docs",
        name="Qt 官方文档",
        description="搜索和读取 Qt 6、Qt Quick、Qt Creator 等官方技术文档。",
        category="开发文档",
        endpoint="https://qt-docs-mcp.qt.io/mcp",
        registry_name="io.qt.qt-docs-mcp/qt-documentation",
        version="1.0.0",
        repository_url=None,
        tools=("qt_documentation_search", "qt_documentation_read"),
    ),
    CatalogMcp(
        id="vonage-docs",
        name="Vonage API 文档",
        description="检索 Vonage API 文档、SDK、教程、代码示例和故障排查资料。",
        category="开发文档",
        endpoint="https://documentation-mcp.vonage.dev/mcp",
        registry_name="io.github.Vonage/vonage-documentation-mcp",
        version="1.0.0",
        repository_url="https://github.com/Vonage/vonage-mcp-server-documentation",
        tools=(
            "vonage_docs_search", "vonage_code_generator", "vonage_api_reference",
            "vonage_troubleshooter", "vonage_tutorial_finder", "vonage_sdk_info",
            "vonage_use_case_examples",
        ),
    ),
)

BY_ID = {item.id: item for item in CATALOG}
MAX_INSTALLED = 10

# MCPs are still loaded through Codex's native config. These rules only decide
# which installed servers should be exposed to a turn when the user did not
# make an explicit selection.
AUTO_ROUTE_PATTERNS = (
    ("weather-data", re.compile(
        r"(?:天气|气温|降雨|下雨|台风|地震|海拔|weather|forecast|earthquake|elevation)", re.I)),
    ("arxiv", re.compile(
        r"(?:arxiv|预印本|论文|学术研究|paper|preprint|literature review)", re.I)),
    ("wikipedia", re.compile(
        r"(?:维基|百科|wikipedia|encyclop(?:a)?edia)", re.I)),
    ("qt-docs", re.compile(r"(?:\bqt(?:\s*6)?\b|qt quick|qml|qt creator)", re.I)),
    ("vonage-docs", re.compile(r"(?:vonage|nexmo)", re.I)),
    ("context7", re.compile(
        r"(?:开发文档|官方文档|接口文档|API\s*(?:文档|reference)|documentation|"
        r"library docs|framework docs|SDK\s*(?:文档|documentation))", re.I)),
    ("exa-search", re.compile(
        r"(?:联网|上网|网页|网站|全网|搜索|查找|检索|新闻|资讯|最新|最近|近期|"
        r"今天|今日|当前|实时|刚刚|现在|web\s*search|search\s+the\s+web|"
        r"latest|recent|current|today|news|online)", re.I)),
)


def installed_ids(db, user_id: str) -> set[str]:
    return set(db.scalars(select(UserMcpInstallation.mcp_id).where(
        UserMcpInstallation.user_id == user_id,
        UserMcpInstallation.enabled.is_(True),
    )).all())


def installed_catalog(db, user_id: str) -> list[CatalogMcp]:
    installed = installed_ids(db, user_id)
    return [item for item in CATALOG if item.id in installed]


def selected_catalog(db, user_id: str, selected_ids: list[str]) -> list[CatalogMcp]:
    """Return selected servers in request order after checking user installation."""
    installed = installed_ids(db, user_id)
    missing = [mcp_id for mcp_id in selected_ids if mcp_id not in installed or mcp_id not in BY_ID]
    if missing:
        raise KeyError(missing[0])
    return [BY_ID[mcp_id] for mcp_id in selected_ids]


def routed_catalog(db, user_id: str, text: str) -> list[CatalogMcp]:
    """Select relevant installed MCPs for a turn, preserving rule priority."""
    installed = installed_ids(db, user_id)
    matches = []
    for mcp_id, pattern in AUTO_ROUTE_PATTERNS:
        if mcp_id in installed and pattern.search(text):
            matches.append(BY_ID[mcp_id])
    return matches


def install(db, user_id: str, mcp_id: str) -> bool:
    if mcp_id not in BY_ID:
        raise KeyError(mcp_id)
    existing = db.scalar(select(UserMcpInstallation).where(
        UserMcpInstallation.user_id == user_id,
        UserMcpInstallation.mcp_id == mcp_id,
    ))
    if existing:
        existing.enabled = True
        return False
    count = len(installed_ids(db, user_id))
    if count >= MAX_INSTALLED:
        raise OverflowError("Too many installed MCP servers")
    db.add(UserMcpInstallation(id=uuid.uuid4().hex, user_id=user_id, mcp_id=mcp_id, enabled=True))
    return True


def uninstall(db, user_id: str, mcp_id: str) -> bool:
    if mcp_id not in BY_ID:
        raise KeyError(mcp_id)
    existing = db.scalar(select(UserMcpInstallation).where(
        UserMcpInstallation.user_id == user_id,
        UserMcpInstallation.mcp_id == mcp_id,
    ))
    if existing is None:
        return False
    db.delete(existing)
    return True


def render_codex_config(base_config: str, servers: list[CatalogMcp]) -> str:
    """Append native Codex MCP tables; no MCP text is added to the task prompt."""
    sections = [base_config.rstrip(), ""]
    for server in servers:
        sections.extend([
            f'[mcp_servers."{server.id}"]',
            f"url = {json.dumps(server.endpoint)}",
            "enabled = true",
            "required = false",
            "startup_timeout_sec = 15",
            "tool_timeout_sec = 60",
            f"enabled_tools = {json.dumps(list(server.tools), ensure_ascii=False)}",
            "",
        ])
    return "\n".join(sections)
