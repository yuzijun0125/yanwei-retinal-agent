import ast
from pathlib import Path
import pytest
from dsml_compat import contains_dsml_action, parse_dsml_calls


def envelope(params, tool='search_cases'):
    return f'<｜DSML｜calls><｜DSML｜invoke name="{tool}">{params}</｜DSML｜invoke></｜DSML｜calls>'


def test_real_screenshot_parameters_execute_once():
    text = envelope('<｜DSML｜parameter name="query_text" string="true">左眼黄斑区眼底出血</｜DSML｜parameter><｜DSML｜parameter name="topk" string="false">3</｜DSML｜parameter><｜DSML｜parameter name="complete_flow" string="false">True</｜DSML｜parameter>')
    calls = []
    exec(parse_dsml_calls(text), {'search_cases': lambda **kw: calls.append(kw), 'print': lambda *_: None})
    assert calls == [dict(query_text='左眼黄斑区眼底出血', topk=3, complete_flow=True)]


def test_string_payload_stays_inert():
    payload = "'); __import__('os').system('bad'); ('"
    code = parse_dsml_calls(envelope(f'<｜DSML｜parameter name="query_text" string="true">{payload}</｜DSML｜parameter>'))
    calls = []
    exec(code, {'search_cases': lambda **kw: calls.append(kw), 'print': lambda *_: None})
    assert calls[0]['query_text'] == payload


@pytest.mark.parametrize('text', [
    envelope('<｜DSML｜parameter name="topk" string="false">__import__("os")</｜DSML｜parameter>'),
    envelope('', 'os.system'), envelope('')[:-5],
    'This is an example: ' + envelope(''),
    envelope('<｜DSML｜parameter name="a">1</｜DSML｜parameter><｜DSML｜parameter name="a">2</｜DSML｜parameter>'),
    envelope('<｜DSML｜parameter name="a"><bad/></｜DSML｜parameter>'),
])
def test_malformed_call_rejected(text):
    assert contains_dsml_action(text)
    with pytest.raises(ValueError):
        parse_dsml_calls(text)


def test_plain_answer_not_a_call():
    assert parse_dsml_calls('请补充病灶位置。') is None
    assert parse_dsml_calls('关于DSML格式的说明') is None


@pytest.mark.parametrize('bars', ['｜｜', '| |', '｜ ｜', '||'])
def test_real_provider_doubled_delimiters(bars):
    sample = envelope('<｜DSML｜parameter name="topk" string="false">3</｜DSML｜parameter>')
    sample = sample.replace('｜DSML｜', bars + 'DSML' + bars + ' ')
    assert contains_dsml_action(sample)
    assert 'topk=3' in parse_dsml_calls(sample)
    assert contains_dsml_action(sample[:-8])


def test_runtime_parser_compatibility_and_fallback_guard():
    source = Path(__file__).with_name('core_agent.runtime.after.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    names = {'parse_code_blobs', '_looks_like_incomplete_action_output', 'contains_dsml_action', 'parse_dsml_calls', '_literal_value'}
    selected = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) and getattr(n, 'module', None) in ('textwrap', 'typing')]
    selected += [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    import re, json, xml.etree.ElementTree as ET
    ns = dict(re=re, json=json, ast=ast, ET=ET, keyword=__import__('keyword'), math=__import__('math'), DSML_TAG_RE=__import__('dsml_compat').DSML_TAG_RE)
    exec(compile(ast.Module(body=selected, type_ignores=[]), '<runtime functions>', 'exec'), ns)
    assert ns['parse_code_blobs']('<code>print(1)</code>') == 'print(1)'
    assert 'search_cases' in ns['parse_code_blobs'](envelope(''))
    assert ns['_looks_like_incomplete_action_output'](envelope('')[:-5]) is True
