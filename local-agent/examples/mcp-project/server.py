"""Official-SDK stdio demo serving synthetic, read-only project material."""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel


server = FastMCP('project_mcp', log_level='WARNING')


class ProjectStatus(BaseModel):
    project_id: str
    owner: str
    completed: int
    total: int
    as_of: str
    synthetic: bool


@server.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=False,
    ),
)
def project_status(project_id: str) -> ProjectStatus:
    """查询合成项目状态；唯一支持的项目编号是 青禾-47。"""
    if project_id != '青禾-47':
        raise ValueError('未知合成项目；请使用项目编号 青禾-47。')
    return ProjectStatus(
        project_id='青禾-47', owner='林澄', completed=3, total=5,
        as_of='2026-09-14', synthetic=True,
    )


@server.resource(
    'project://qinghe/notes',
    name='project_notes',
    description='青禾-47 的固定合成项目笔记，包含负责人和进展。',
    mime_type='text/plain',
)
def project_notes() -> str:
    return (
        '以下全部为 MCP 演示用合成资料，不对应真实项目。\n'
        '项目：青禾-47\n'
        '负责人：林澄\n'
        '记录日期：2026-09-14\n'
        '任务进展：共 5 项，完成 3 项，剩余 2 项。\n'
        '已完成：需求整理、接口草案、页面原型。\n'
        '待完成：联调验证、验收记录。\n'
    )


@server.prompt()
def project_brief(audience: str) -> str:
    """提供面向指定受众的简报方法模板；不包含项目事实或访问授权。"""
    if not audience.strip() or len(audience) > 80 or any(c in audience for c in '\r\n'):
        raise ValueError('受众须为 1–80 个字符的单行文字。')
    return (
        f'目标受众：{audience.strip()}。\n'
        '简报方法：先确认项目编号，再依据本次实际取得的资料，'
        '按“负责人、完成进展、待办、资料日期”组织内容。\n'
        '保留编号和数字原文，注明资料来源；缺失信息明确写“资料未提供”。\n'
        '此模板只提供组织方法，不提供项目事实，也不授予工具或资料访问权限。\n'
    )


if __name__ == '__main__':
    server.run(transport='stdio')
