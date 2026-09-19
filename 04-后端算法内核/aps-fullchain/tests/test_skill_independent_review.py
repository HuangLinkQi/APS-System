import copy
import unittest
from aps_fullchain.fixtures.scenarios import create_micro_scenario
from aps_fullchain.simulator import ExecutionWorld
from aps_fullchain.policies import get_strategy_a
from aps_fullchain.validate import validate_trace
from aps_fullchain.schema import LaborSkillPool

class SkillReview(unittest.TestCase):
    def setUp(self):
        self.sc = create_micro_scenario('SKILL_REVIEW')
        world = ExecutionWorld(calendar=self.sc.calendar, configurations=self.sc.configurations,
            resources=self.sc.resources, buffers=self.sc.buffers, labor=self.sc.labor,
            initial_inventory=self.sc.initial_inventory, deliveries=self.sc.deliveries,
            orders={o.vin:o.clone() for o in self.sc.orders},stage_policies=get_strategy_a(), horizon_end_min=480)
        self.trace=world.run(stop_at_min=480)
        self.assertTrue(validate_trace(self.sc,self.trace).is_valid)
    def bind_weld(self):
        self.sc.labor.skill_pools['REVIEW_WELD']=LaborSkillPool('REVIEW_WELD','REVIEW_WELD',99)
        for cfg in self.sc.configurations.values():
            cfg.skill_requirements.setdefault('W',{})['processing']='REVIEW_WELD'
        return self.sc.labor.skill_pools['REVIEW_WELD']
    def test_skill_zero_even_if_total_sufficient(self):
        pool=self.bind_weld();pool.max_workers=0
        errors=validate_trace(self.sc,self.trace).errors
        self.assertTrue(any('Skill capacity exceeded for REVIEW_WELD' in e for e in errors),errors)
    def test_capacity_drop_inside_operation(self):
        pool=self.bind_weld()
        nodes=next(v['W'] for v in self.trace.stage_node_times.values() if 'W' in v and v['W'].get('C',0)-v['W'].get('S',0)>2)
        pool.time_windows=[(nodes['S']+1,nodes['C']-1,0)]
        self.assertTrue(any('Skill capacity exceeded' in e for e in validate_trace(self.sc,self.trace).errors))
    def test_total_drop_inside_operation(self):
        n=next(v['W'] for v in self.trace.stage_node_times.values() if 'W' in v and v['W'].get('C',0)-v['W'].get('S',0)>2)
        self.sc.labor.time_windows=[(n['S']+1,n['C']-1,0)]
        self.assertTrue(any('Time-varying labor capacity exceeded' in e for e in validate_trace(self.sc,self.trace).errors))
    def test_unknown_skill_rejected(self):
        self.bind_weld();del self.sc.labor.skill_pools['REVIEW_WELD']
        self.sc.labor.skill_pools['OTHER']=LaborSkillPool('OTHER','OTHER',99)
        self.assertTrue(any('Unknown labor skill' in e for e in validate_trace(self.sc,self.trace).errors))
