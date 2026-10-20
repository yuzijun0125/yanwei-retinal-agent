"""Provision only this integration's MCP/agent through authenticated Nexent APIs."""
import json
import os
from pathlib import Path
import threading
from gateway import CONFIG, NEXENT, upstream_json

_LOCK = threading.Lock()
TOOLS = {'extract_medical_entities', 'search_cases', 'verify_medical_logic',
         'build_evidence_chain', 'record_feedback', 'read_medical_record'}


def rows(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ('data', 'tools', 'mcp_servers', 'mcp_list', 'remote_mcp_server_list', 'list', 'models', 'model_list', 'items'):
            if key in value:
                result = rows(value[key])
                if result:
                    return result
    return []


def find_id(value, key):
    if type(value) is int:
        return value
    if isinstance(value, dict):
        if type(value.get(key)) is int:
            return value[key]
        if type(value.get('id')) is int:
            return value['id']
        return find_id(value.get('data'), key)
    return None


def provision(model_id, headers):
    def api(path, data=None, method=None):
        return upstream_json(NEXENT, path, method or ('POST' if data is not None else 'GET'), data, headers)
    with _LOCK:
        # The login is exercised before writing any local state.
        agents = rows(api('/api/agent/list'))
        services = rows(api('/api/mcp/list'))
        mcp_name = 'yanwei_integrated_mcp'
        endpoint = os.environ.get('MCP_PUBLIC_URL', 'http://yanwei-retrieval:8235/mcp')
        existing = next((r for r in services if r.get('name', r.get('mcp_server_name', r.get('remote_mcp_server_name'))) == mcp_name), None)
        if existing:
            mcp_id = find_id(existing, 'mcp_id')
            known_url = existing.get('server_url', existing.get('mcp_url', existing.get('remote_mcp_server')))
            if known_url and known_url != endpoint:
                raise ValueError('同名 MCP 地址不同，请在 Nexent 中核对后重试；未覆盖现有服务')
        else:
            added = api('/api/mcp/add', {'name': mcp_name, 'server_url': endpoint, 'source': 'local',
                                       'enabled': True, 'description': '眼维一体化部署的六个病历识别、检索与证据工具'})
            mcp_id = find_id(added, 'mcp_id')
            if not mcp_id:
                service = next((r for r in rows(api('/api/mcp/list')) if r.get('remote_mcp_server_name', r.get('name')) == mcp_name), None)
                mcp_id = find_id(service, 'mcp_id')
        if not mcp_id:
            raise ValueError('MCP 初始化未返回 ID，请查看平台服务列表')
        api('/api/tool/scan_tool')
        api('/api/mcp/tools?mcp_id=' + str(mcp_id))
        available = [t for t in rows(api('/api/tool/list')) if t.get('usage') == mcp_name]
        selected = [t for t in available if t.get('name', t.get('tool_name')) in TOOLS]
        ids = [find_id(t, 'tool_id') for t in selected]
        if len(selected) != len(TOOLS) or not all(ids):
            raise ValueError('六个 MCP 工具尚未完整注册，请检查服务健康和工具同步')
        name = 'yanwei_integrated'
        agent = next((a for a in agents if a.get('name') == name), None)
        agent_id = find_id(agent, 'agent_id') if agent else find_id(api('/api/agent/get_creating_sub_agent_id'), 'agent_id')
        if not agent_id:
            raise ValueError('平台没有返回新智能体 ID')
        duty = '''你是眼维眼底图文相似病例检索助手。支持自然中文短语与多轮补充。
每轮有[YANWEI_ROUTE] JSON，request_id是本轮网关生成的跟踪ID，image_asset_id是已真实上传的图片ID。
若YANWEI_ROUTE含非空record_asset_id，本轮只调用read_medical_record，逐字传入record_asset_id和request_id；识别成功只简短提示用户核对，原文由前端核对框显示；不要复述整篇病历，也不要自行补全内容。本轮不要检索。
用户确认或修改识别内容后，再根据本轮确认文字检索，不能继续使用旧OCR文字。
进行病例检索时仅调用一次search_cases，complete_flow=true，topk=3，必须逐字传入本轮request_id及非空image_asset_id。
query_text须保留用户原始眼别、否定、不确定、病灶和位置。根据历史补全用户已明确的信息；不能擅自选单眼。
用户明确选择一眼时，用本轮选择替换前轮冲突的眼别，并保留病灶位置。新完整查询不要混入旧病灶。
ASK_MORE时按工具结果追问；ABSTAIN时明确证据不足；RETURN_CASES时简洁说明返回参考病例。
工具返回的动作、候选排序和版本是唯一依据。研究用，无独立医学有效性验证。'''
        constraints = '''不得编造、猜测或复用旧request_id/图片资产ID。问候和能力介绍可以直接回复，不能声称已检索。
complete_flow已包含解析/检索/门禁/证据，不要重复调用extract或其他流程工具。
有明确的图片ID必须交给search_cases，不要仅根据文件名猜测图像内容。
没有实际工具成功返回不得声称检索完成。工具失败应简要报告，不得拿历史结果充当本轮。
图片相似度不是诊断概率，不作确诊或治疗建议；详细候选和证据由工作台显示。'''
        api('/api/agent/update', {'agent_id': agent_id, 'name': name, 'display_name': '眼维 · 一体化图文检索',
                                'description': 'Nexent实际编排、MCP图文检索与可核验决策面板',
                                'duty_prompt': duty, 'constraint_prompt': constraints, 'max_steps': 6,
                                'requested_output_tokens': 2048, 'model_ids': [model_id],
                                'enabled_tool_ids': ids, 'enabled': True, 'is_main_agent': True})
        published = api(f'/api/agent/{agent_id}/publish', {'version_name': '眼维一体化', 'release_note': '同源网关与请求级MCP结果关联'})
        version = find_id(published, 'version_no')
        if not version:
            detail = api('/api/agent/by-name/' + name)
            version = detail.get('version_no', detail.get('current_version_no')) if isinstance(detail, dict) else None
        if not version:
            raise ValueError('发布未返回有效版本，未启用草稿冒充发布版')
        result = {'agent_id': agent_id, 'model_id': model_id, 'version_no': version, 'mcp_id': mcp_id}
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        temp = CONFIG.with_suffix('.tmp')
        temp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        temp.replace(CONFIG)
        return result
