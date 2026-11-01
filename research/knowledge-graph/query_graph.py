"""Read-only graph queries: explicit paths, no free-form medical generation."""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def query(graph, text, limit=5):
    nodes = {item['id']: item for item in graph['nodes']}
    edges = graph['edges']
    if any(term in text for term in ('治疗', '吃药', '用药', '确诊', '剂量', '注射')):
        return {'status': 'OUT_OF_SCOPE', 'answer': '该原型仅查询资料和基础关系，不提供诊断、治疗或用药结论。', 'paths': []}
    aliases = {'MA': ('微血管瘤', '微动脉瘤'), 'HE': ('出血',), 'EX': ('硬性渗出',), 'SE': ('棉絮斑', '软性渗出')}
    targets = [code for code, terms in aliases.items() if any(term in text for term in terms)]
    if len(targets) > 1 or any(term in text for term in ('未见', '没有', '疑似', '不排除', '无出血', '无微血管瘤')):
        return {'status': 'NEEDS_CLARIFICATION', 'answer': '此规则小样暂不处理多病灶、否定或疑似查询，请选择单一已确认病灶；没有把否定改成阳性。', 'paths': []}
    case_ids = [key for key in nodes if key.startswith('CASE_') and key in text]
    if case_ids:
        case_id = case_ids[0]
        selected = [edge for edge in edges if edge['source'] == case_id]
        image_ids = {edge['target'] for edge in selected if edge['relation'] == 'HAS_IMAGE'}
        selected += [edge for edge in edges if edge['source'] in image_ids]
        regions = {edge['target'] for edge in selected if edge['relation'] == 'HAS_REGION'}
        selected += [edge for edge in edges if edge['source'] in regions]
        return {'status': 'RECORD_TRACE', 'case_id': case_id, 'answer': '返回该项目病例记录的影像、区域和字段来源；不代表新增诊断。', 'paths': selected}
    if targets and any(term in text for term in ('病例', '参考', '查找', '黄斑')):
        required = {'lesion:' + code for code in targets}
        matches = {}
        for edge in edges:
            if edge['relation'] == 'ANNOTATED_AS' and edge['target'] in required:
                matches.setdefault(edge['source'], set()).add(edge['target'])
        regions = {region for region, labels in matches.items() if required <= labels}
        if '黄斑' in text:
            regions &= {edge['source'] for edge in edges if edge['relation'] == 'PROJECT_LOCATION' and edge['target'] == 'anatomy:MACULA'}
        # Keep all filters on the same region; do not borrow a location from a different region.
        rows = []
        seen = set()
        for region in sorted(regions):
            region_edge = next(edge for edge in edges if edge['relation'] == 'HAS_REGION' and edge['target'] == region)
            image = region_edge['source']
            case_edge = next(edge for edge in edges if edge['relation'] == 'HAS_IMAGE' and edge['target'] == image)
            if image in seen:
                continue
            seen.add(image)
            support = [edge for edge in edges if edge['source'] == region and (edge['relation'] == 'ANNOTATED_AS' or ('黄斑' in text and edge['relation'] == 'PROJECT_LOCATION'))]
            rows.append({'case_id': case_edge['source'], 'image_id': image, 'region_id': region,
                         'path': [case_edge, region_edge, *support], 'review_required': any(edge['status'] == 'REVIEW_REQUIRED' for edge in support)})
        return {'status': 'REFERENCE_RECORDS', 'count': len(rows), 'candidates': rows[:limit],
                'answer': '结果来自参考库已有标注；位置为项目推导、待复核，不能据此诊断新图片。'}
    if '糖尿病' in text or '糖网病' in text:
        selected = [edge for edge in edges if edge['source'] in {'disease:DR', 'disease:DIABETES'}]
        return {'status': 'BASIC_SOURCE_RELATIONS', 'answer': '返回有公开来源的基础关系，未使用大模型生成答案，也不包含诊疗建议。', 'paths': selected}
    return {'status': 'NOT_FOUND', 'answer': '当前图谱未覆盖该问题，不补编关系。可查“黄斑区出血参考病例”“糖尿病视网膜病变”或病例ID。', 'paths': []}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('question')
    parser.add_argument('--limit', type=int, default=5)
    args = parser.parse_args()
    graph = json.loads((HERE / 'artifacts/graph.json').read_text(encoding='utf-8'))
    print(json.dumps(query(graph, args.question, max(1, min(args.limit, 30))), ensure_ascii=False, indent=2))
