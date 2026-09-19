"""Versioned demonstration master data and the single scheduling input contract."""
import copy
import math
import xml.etree.ElementTree as ET

STAGES = {'W': '焊装', 'P': '涂装', 'A': '总装'}


def extract_master(xml):
    from xml_adapter import parse_xml_to_scenario
    sc = parse_xml_to_scenario(xml)
    return {
        'resources': [{'id': r.resource_id, 'stage': r.stage, 'enabled': True} for r in sc.resources.values()],
        'times': [{'config': c.config_id, 'model': c.model_type, 'color': c.color, 'stage': s, 'minutes': c.process_times.get(s)} for c in sc.configurations.values() for s in c.stages],
        'shifts': [{'id': s.shift_id, 'day': s.day_idx + 1, 'start': s.start_min - s.day_idx * sc.calendar.day_length_min, 'end': s.end_min - s.day_idx * sc.calendar.day_length_min} for s in sc.calendar.shifts],
        'buffers': [{'id': b.buffer_id, 'edge': b.edge, 'capacity': b.capacity, 'minutes': b.transit_time_min} for b in sc.buffers.values()],
        'setups': [{'config': c.config_id, 'stage': s, 'pair': pair, 'minutes': minutes} for c in sc.configurations.values() for s, matrix in c.setup_matrix.items() for pair, minutes in matrix.items()],
        'horizon': sc.calendar.horizon_end_min, 'day_length': sc.calendar.day_length_min,
    }


def validate_master(data):
    def number(v, label, minimum=0, blank=False):
        if blank and (v is None or v == ''):
            return
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or int(v) != v or v < minimum:
            raise ValueError(label + f'须为不小于{minimum}的整数')
    if not isinstance(data, dict):
        raise ValueError('基础资料格式错误')
    for key in ('resources', 'times', 'shifts', 'buffers', 'setups'):
        if not isinstance(data.get(key), list):
            raise ValueError('缺少基础资料：' + key)
    ids = set()
    for r in data['resources']:
        if not isinstance(r.get('id'), str) or not r['id'].strip() or r['id'] in ids:
            raise ValueError('产线编号必填且不能重复')
        ids.add(r['id'])
        if r.get('stage') not in STAGES or not isinstance(r.get('enabled'), bool):
            raise ValueError('产线工序或启用状态无效')
    for t in data['times']:
        number(t.get('minutes'), t['config'] + ' ' + STAGES.get(t['stage'], t['stage']) + '节拍（分钟）', 1, True)
    windows = []
    ids = set()
    for s in data['shifts']:
        if not s.get('id') or s['id'] in ids:
            raise ValueError('班次编号必填且不能重复')
        ids.add(s['id'])
        number(s.get('day'), '班次日期', 1)
        number(s.get('start'), '班次开始分钟')
        number(s.get('end'), '班次结束分钟', 1)
        start = (s['day'] - 1) * data['day_length'] + s['start']
        end = (s['day'] - 1) * data['day_length'] + s['end']
        if s['start'] >= s['end'] or s['end'] > data['day_length'] or end > data['horizon']:
            raise ValueError('班次开始必须早于结束，且不能超出当日或计算时域')
        if any(start < b and end > a for a, b in windows):
            raise ValueError('班次时间不能重叠')
        windows.append((start, end))
    for b in data['buffers']:
        number(b.get('capacity'), b['id'] + '缓冲容量', 1)
        number(b.get('minutes'), b['id'] + '转运分钟')
    for s in data['setups']:
        number(s.get('minutes'), s['config'] + '切换分钟')


def readiness(data):
    issues = []
    for stage in STAGES:
        active = [r for r in data['resources'] if r['stage'] == stage and r['enabled']]
        if len(active) != 1:
            issues.append(STAGES[stage] + '需恰好1条启用产线；当前引擎不支持同工序多产线调度')
    for t in data['times']:
        if t.get('minutes') in (None, ''):
            issues.append(t['config'] + '缺少' + STAGES[t['stage']] + '有效节拍')
    if not data['shifts']:
        issues.append('缺少有效生产班次')
    return issues


def apply_master(xml, data):
    root = ET.fromstring(xml)
    resources = root.find('ShopResources')
    previous = {x.findtext('ResourceId'): copy.deepcopy(x) for x in resources}
    resources.clear()
    for r in data['resources']:
        if not r['enabled']:
            continue
        elem = previous.get(r['id'])
        if elem is None:
            elem = ET.Element('Resource')
            for k, v in [('ResourceId', r['id']), ('Stage', r['stage']), ('Capacity', 1), ('InitialColor', 'NONE'), ('InitialModelType', 'NONE')]:
                ET.SubElement(elem, k).text = str(v)
        if elem.find('Capacity') is None:
            ET.SubElement(elem, 'Capacity').text = '1'
        else:
            elem.find('Capacity').text = '1'
        elem.find('Stage').text = r['stage']
        resources.append(elem)
    shifts = root.find('FactoryCalendar/Shifts')
    shifts.clear()
    for s in data['shifts']:
        el = ET.SubElement(shifts, 'Shift')
        for k, v in [('ShiftId', s['id']), ('DayIndex', s['day'] - 1), ('StartMin', (s['day'] - 1) * data['day_length'] + s['start']), ('EndMin', (s['day'] - 1) * data['day_length'] + s['end'])]:
            ET.SubElement(el, k).text = str(v)
    for c in root.findall('VehicleConfigurations/Configuration'):
        cid = c.findtext('ConfigId')
        for t in c.findall('ProcessTimes/StageTime'):
            row = next(x for x in data['times'] if x['config'] == cid and x['stage'] == t.get('stage'))
            t.set('durationMin', str(row['minutes'] or 0))
        old = c.find('SetupMatrix')
        if old is not None:
            c.remove(old)
        matrix = ET.SubElement(c, 'SetupMatrix')
        for s in data['setups']:
            if s['config'] == cid:
                ET.SubElement(matrix, 'Setup', stage=s['stage'], pair=s['pair'], durationMin=str(s['minutes']))
    for b in root.findall('InterStageBuffers/Buffer'):
        row = next(x for x in data['buffers'] if x['id'] == b.findtext('BufferId'))
        b.find('Capacity').text = str(row['capacity'])
        b.find('TransitTimeMin').text = str(row['minutes'])
    return ET.tostring(root, encoding='unicode')


def display_input(inp):
    from xml_adapter import parse_xml_to_scenario
    sc = parse_xml_to_scenario(inp['raw_xml'])
    return {
        'orders': [{'订单': o.order_id, '车辆': o.vin, '配置': o.config_id, '工艺路径': ' → '.join(getattr(o, 'route', None) or ['W', 'P', 'A']), '优先级': o.priority, '释放分钟': o.release_at_min, '交期分钟': o.due_at_min} for o in sc.orders],
        'stock': [{'物料': k, '初始可用数量': v} for k, v in sc.initial_inventory.items()],
        'deliveries': [{'物料': d.material_id, '数量': d.quantity, '到货分钟': d.available_at_min, '已知分钟': d.known_at_min} for d in sc.deliveries],
        'events': [{'事件': e.event_type, '发生分钟': e.occurred_at_min, '已知分钟': e.known_at_min} for e in sc.events],
        'labor': [{'人员池': sc.labor.pool_id, '总人数': sc.labor.max_workers}] + [{'人员池': p.pool_id, '技能': p.skill_name, '人数': p.max_workers} for p in sc.labor.skill_pools.values()],
        'master': inp.get('master'), 'checks': inp.get('checks', []),
    }
