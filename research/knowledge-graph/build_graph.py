"""Build an evidence-bearing retinal graph from existing, unreviewed metadata.

This is a deterministic data baseline, not a reproduction of the thesis LLM/MLP agents.
"""
import argparse
import base64
import collections
import csv
import hashlib
import io
import json
from pathlib import Path
import sqlite3

HERE = Path(__file__).resolve().parent
DEFAULT_SOURCE = HERE / 'inputs'
NAMES = {'MA': '微血管瘤', 'HE': '出血', 'EX': '硬性渗出', 'SE': '棉絮斑',
         'MACULA': '黄斑区', 'POSTERIOR_POLE': '后极部', 'FOVEA': '黄斑中心',
         'TEMPORAL': '颞侧', 'NASAL': '鼻侧', 'DISC': '视盘', 'CFP': '眼底彩照',
         'OD': '右眼', 'OS': '左眼', 'SCATTERED': '散在（项目程度草案）',
         'MODERATE': '中度（项目程度草案）', 'MARKED': '较重（项目程度草案）',
         'MILD': '少量（项目程度草案）'}
RELATIONS = {
    'HAS_IMAGE': ('CaseRecord', 'ImageAsset', '关联影像'),
    'HAS_REGION': ('ImageAsset', 'RegionAnnotation', '含参考标注区域'),
    'ANNOTATED_AS': ('RegionAnnotation', 'LesionConcept', '已有标注类型'),
    'PROJECT_LOCATION': ('RegionAnnotation', 'Anatomy', '项目推导位置'),
    'PROJECT_SEVERITY': ('RegionAnnotation', 'SeverityDraft', '项目程度草案'),
    'PROJECT_EYE': ('ImageAsset', 'Laterality', '项目推导眼别'),
    'MODALITY': ('ImageAsset', 'Modality', '影像模态'),
    'HAS_TEXT': ('CaseRecord', 'TextRecord', '关联文本记录'),
    'MENTIONS': ('TextRecord', 'LesionConcept', '文字提及（不代表肯定存在）'),
    'HAS_COMPLICATION': ('Disease', 'Disease', '可发生的并发症'),
    'AFFECTS': ('Disease', 'Anatomy', '涉及部位'),
    'ASSOCIATED_LESION': ('Disease', 'LesionConcept', '文献中的相关病灶'),
}


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def build(source):
    data_root = source / 'silver' if (source / 'silver').is_dir() else source
    nodes, edges = {}, {}
    logs = []
    hashes = {}
    def node(identifier, label, kind, **props):
        candidate = {'id': identifier, 'label': label, 'type': kind, 'properties': props}
        if identifier in nodes and nodes[identifier]['type'] != kind:
            raise ValueError('Node type conflict: ' + identifier)
        nodes.setdefault(identifier, candidate)
        return identifier

    def edge(head, relation, tail, evidence, status='IMPORTED_PENDING'):
        domain, range_, _ = RELATIONS[relation]
        if nodes[head]['type'] != domain or nodes[tail]['type'] != range_:
            raise ValueError('Invalid domain/range: ' + relation)
        if not evidence.get('locator') or not evidence.get('excerpt'):
            raise ValueError('Missing evidence')
        key = digest([head, relation, tail])[:24]
        item = edges.setdefault(key, {'id': key, 'source': head, 'target': tail, 'relation': relation,
                                     'label': RELATIONS[relation][2], 'status': status, 'evidence': []})
        if evidence not in item['evidence']:
            item['evidence'].append(evidence)

    def evidence(name, row, index, excerpt, kind='PROJECT_METADATA'):
        return {'document': name, 'locator': f'{name}:CSV行{index + 2}', 'record_hash': row.get('record_hash', ''),
                'file_sha256': hashes[name], 'excerpt': excerpt,
                'official_source': row.get('official_source', ''), 'transform': row.get('transform', ''),
                'original_review_status': row.get('review_status', 'UNKNOWN'), 'kind': kind}

    tables = {}
    snapshot = HERE / 'inputs'
    snapshot.mkdir(exist_ok=True)
    for name in ['silver_case.csv', 'silver_image.csv', 'silver_region.csv', 'silver_lesion.csv', 'silver_text.csv']:
        raw = (data_root / name).read_bytes()
        (snapshot / name).write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
        tables[name] = read_csv(data_root / name)
    logs.append({'stage': 'structured_import', 'engine': 'deterministic_csv_adapter', 'llm_called': False,
                 'rows': {name: len(rows) for name, rows in tables.items()}})

    for row in tables['silver_case.csv']:
        node(row['case_id'], row['case_id'], 'CaseRecord',
             note='项目按图像聚合的病例记录；不是81位已确认真实患者', patient_key=row.get('patient_key', 'UNKNOWN'))
    images = {}
    for i, row in enumerate(tables['silver_image.csv']):
        image = node(row['image_id'], row.get('source_ref') or row['image_id'], 'ImageAsset',
                     filename=Path(row.get('raw_path', '')).name, split=row.get('split'), sha256=row.get('sha256'),
                     original_review_status=row.get('review_status'))
        images[image] = row
        edge(row['case_id'], 'HAS_IMAGE', image, evidence('silver_image.csv', row, i, f'{row["image_id"]} → {row["case_id"]}'))
        modality = row.get('modality')
        if modality and modality != 'UNKNOWN':
            target = node('modality:' + modality, NAMES.get(modality, modality), 'Modality')
            edge(image, 'MODALITY', target, evidence('silver_image.csv', row, i, 'modality=' + modality))
        eye = row.get('eye')
        if eye and eye != 'UNKNOWN':
            target = node('eye:' + eye, NAMES.get(eye, eye), 'Laterality')
            edge(image, 'PROJECT_EYE', target, evidence('silver_image.csv', row, i, f'eye={eye}; eye_source={row.get("eye_source")}'), 'REVIEW_REQUIRED')

    for i, row in enumerate(tables['silver_region.csv']):
        region = node(row['region_id'], row['region_id'], 'RegionAnnotation', bbox=row.get('bbox'),
                      area_ratio=row.get('area_ratio'), mask_path=row.get('mask_path'),
                      note='参考库既有标注的项目区域；不是新上传影像预测')
        edge(row['image_id'], 'HAS_REGION', region, evidence('silver_region.csv', row, i, f'image_id={row["image_id"]}; region_id={region}'))
        code = row.get('lesion_type')
        if code and code != 'UNKNOWN':
            target = node('lesion:' + code, NAMES.get(code, code), 'LesionConcept', code=code)
            edge(region, 'ANNOTATED_AS', target, evidence('silver_region.csv', row, i, 'lesion_type=' + code, 'MASK_DERIVED_METADATA'))
        location = row.get('location')
        if location and location != 'UNKNOWN':
            target = node('anatomy:' + location, NAMES.get(location, location), 'Anatomy')
            edge(region, 'PROJECT_LOCATION', target, evidence('silver_region.csv', row, i, f'location={location}; location_source={row.get("location_source")}'), 'REVIEW_REQUIRED')
    for i, row in enumerate(tables['silver_lesion.csv']):
        region, code = row.get('region_id'), row.get('severity')
        if region in nodes and code and code != 'UNKNOWN':
            target = node('severity:' + code, NAMES.get(code, code + '（项目程度草案）'), 'SeverityDraft')
            edge(region, 'PROJECT_SEVERITY', target, evidence('silver_lesion.csv', row, i, f'severity={code}; severity_source={row.get("severity_source")}'), 'REVIEW_REQUIRED')

    for i, row in enumerate(tables['silver_text.csv']):
        text = row.get('text_span', '')
        if not text.strip() or row.get('case_id') not in nodes:
            continue
        identifier = node(row['text_id'], row['text_id'], 'TextRecord', text=text,
                          text_type=row.get('text_type'), text_origin=row.get('text_origin'),
                          note='仅保留来源文本，合成文本不得当作真实临床报告')
        edge(row['case_id'], 'HAS_TEXT', identifier, evidence('silver_text.csv', row, i, text[:150], row.get('text_origin') or 'PROJECT_TEXT'))
        for code, label in [('MA', '微血管瘤'), ('HE', '出血'), ('EX', '硬性渗出'), ('SE', '棉絮斑')]:
            if label in text:
                target = node('lesion:' + code, NAMES[code], 'LesionConcept', code=code)
                edge(identifier, 'MENTIONS', target, evidence('silver_text.csv', row, i, text[:150], 'LEXICAL_MENTION_ONLY'), 'MENTION_ONLY')
    logs.append({'stage': 'text_mentions', 'engine': 'dictionary_mentions_not_clinical_relation_extraction',
                 'llm_called': False, 'note': '否定和疑似文本也只是提及，绝不自动写成存在病灶'})

    diabetes = node('disease:DIABETES', '糖尿病', 'Disease')
    dr = node('disease:DR', '糖尿病视网膜病变', 'Disease')
    retina = node('anatomy:RETINA', '视网膜', 'Anatomy')
    nei = {'document': 'NEI患者教育手册（仅用于基本概念）', 'locator': 'PDF第5页 / 正文第1页',
           'url': 'https://www.nei.nih.gov/sites/default/files/health-pdfs/diabeticretino.pdf',
           'excerpt': '人工概括：糖尿病视网膜病变是糖尿病的并发症，涉及视网膜微小血管。',
           'kind': 'MANUALLY_CURATED_PUBLIC_SOURCE', 'original_review_status': 'NO_LOCAL_CLINICAL_REVIEW',
           'note': '历史科普资料；本样例不采入治疗与随访建议，不宣称最新诊疗标准'}
    edge(diabetes, 'HAS_COMPLICATION', dr, nei, 'SOURCE_BACKED_NOT_EXPERT_REVIEWED')
    edge(dr, 'AFFECTS', retina, nei, 'SOURCE_BACKED_NOT_EXPERT_REVIEWED')
    mdpi = {'document': 'IDRiD原始数据论文（2018）', 'locator': 'Figure 1 / Figure 2 captions',
            'url': 'https://www.mdpi.com/2306-5729/3/3/25',
            'excerpt': '人工概括：论文示例展示糖网病相关的微血管瘤、出血、硬性渗出和软性渗出/棉絮斑。',
            'kind': 'MANUALLY_CURATED_PUBLIC_SOURCE', 'original_review_status': 'NO_LOCAL_CLINICAL_REVIEW',
            'note': '只表示文献相关性；不能单凭其中一个病灶诊断糖网病'}
    for code in ('MA', 'HE', 'EX', 'SE'):
        edge(dr, 'ASSOCIATED_LESION', 'lesion:' + code, mdpi, 'SOURCE_BACKED_NOT_EXPERT_REVIEWED')
    logs.append({'stage': 'medical_seed', 'engine': 'manual_source_curation', 'llm_called': False, 'facts': 6})

    graph = {'schema_version': 'yanwei-kg/0.1', 'clinical_validity': 'UNVALIDATED',
             'method': 'deterministic_baseline_with_manual_medical_seed',
             'nodes': sorted(nodes.values(), key=lambda row: row['id']),
             'edges': sorted(edges.values(), key=lambda row: row['id']),
             'ontology': {key: {'domain': value[0], 'range': value[1], 'label': value[2]} for key, value in RELATIONS.items()},
             'input_sha256': hashes, 'stages': logs}
    graph['stats'] = {'nodes': len(nodes), 'edges': len(edges),
                      'node_types': dict(collections.Counter(row['type'] for row in nodes.values())),
                      'edge_status': dict(collections.Counter(row['status'] for row in edges.values())),
                      'review_required': sum(row['status'] == 'REVIEW_REQUIRED' for row in edges.values()),
                      'medical_seed_facts': 6, 'llm_calls': 0, 'trained_mlp': False}
    return graph


def save(graph):
    output = HERE / 'artifacts'
    output.mkdir(exist_ok=True)
    (output / 'graph.json').write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding='utf-8')
    with (output / 'nodes.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream); writer.writerow(['id', 'name', 'type', 'properties'])
        for item in graph['nodes']:
            writer.writerow([item['id'], item['label'], item['type'], json.dumps(item['properties'], ensure_ascii=False)])
    with (output / 'relations.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream); writer.writerow(['id', 'head', 'relation', 'tail', 'status', 'evidence'])
        for item in graph['edges']:
            writer.writerow([item['id'], item['source'], item['relation'], item['target'], item['status'], json.dumps(item['evidence'], ensure_ascii=False)])
    db_path = output / 'graph.sqlite'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, label TEXT, type TEXT, payload TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS edges (id TEXT PRIMARY KEY, head TEXT REFERENCES nodes(id), relation TEXT, tail TEXT REFERENCES nodes(id), status TEXT, payload TEXT)')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('DELETE FROM edges'); db.execute('DELETE FROM nodes')
        db.executemany('INSERT INTO nodes VALUES (?,?,?,?)', [(n['id'], n['label'], n['type'], json.dumps(n, ensure_ascii=False)) for n in graph['nodes']])
        db.executemany('INSERT INTO edges VALUES (?,?,?,?,?,?)', [(e['id'], e['source'], e['relation'], e['target'], e['status'], json.dumps(e, ensure_ascii=False)) for e in graph['edges']])
        db.execute('CREATE INDEX IF NOT EXISTS edges_relation ON edges(relation, tail)')
    with (output / 'review_queue.json').open('w', encoding='utf-8') as stream:
        json.dump([e for e in graph['edges'] if e['status'] == 'REVIEW_REQUIRED'], stream, ensure_ascii=False, indent=2)
    (output / 'build_report.json').write_text(json.dumps(graph['stats'], ensure_ascii=False, indent=2), encoding='utf-8')
    template = (HERE / 'viewer_template.html').read_text(encoding='utf-8')
    embedded = json.dumps(graph, ensure_ascii=False).replace('<', '\\u003c')
    (HERE / '眼维知识图谱.html').write_text(template.replace('__GRAPH_JSON__', embedded), encoding='utf-8')
    (output / 'ontology.json').write_text(json.dumps(graph['ontology'], ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE if DEFAULT_SOURCE.is_dir() else HERE / 'inputs')
    args = parser.parse_args()
    graph = build(args.source)
    save(graph)
    print(json.dumps(graph['stats'], ensure_ascii=False, indent=2))
