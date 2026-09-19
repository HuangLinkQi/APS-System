"""Independent runtime audit: accepted-plan assignment versus actual W/B starts."""
import copy,json
from pathlib import Path
from aps_fullchain import experiments
from aps_fullchain.simulator import ExecutionWorld

worlds=[]
class AuditWorld(ExecutionWorld):
    def __init__(self,*args,**kwargs):
        self.review_plans=[]
        super().__init__(*args,**kwargs)
        worlds.append(self)
        plan=kwargs.get('order_plan')
        if plan is not None:
            self.review_plans.append({'time':0,'event_index':0,'plan_id':plan.plan_id,'allocations':copy.deepcopy(plan.daily_allocations)})
    def __setattr__(self,key,value):
        super().__setattr__(key,value)
        if key=='authorized_vins' and hasattr(self,'trace') and hasattr(self,'orders') and value is not None:
            allocations={}
            for vin in value:
                order=self.orders.get(vin)
                if order is not None and order.assigned_day is not None:
                    allocations.setdefault(order.assigned_day,[]).append(vin)
            self.review_plans.append({'time':self.current_time_min,'event_index':len(self.trace.records),'plan_id':'runtime-authorized-scope','allocations':allocations})
    def set_active_plan(self,plan):
        self.review_plans.append({'time':self.current_time_min,'event_index':len(self.trace.records),
          'plan_id':plan.plan_id,'version':getattr(plan,'version',None),
          'allocations':copy.deepcopy(plan.daily_allocations)})
        return super().set_active_plan(plan)

def main():
    original=experiments.ExecutionWorld
    experiments.ExecutionWorld=AuditWorld
    try:matrix=experiments.run_all_scenarios_matrix()
    finally:experiments.ExecutionWorld=original
    audits=[];failures=[]
    for index,w in enumerate(worlds):
        if not w.review_plans:continue
        starts=0
        for ei,record in enumerate(w.trace.records):
            if record.stage!='W' or record.node!='B':continue
            starts+=1
            active=[p for p in w.review_plans if p['event_index']<=ei and p['time']<=record.timestamp_min]
            if not active:
                failures.append({'world':index,'vin':record.vin,'error':'no effective plan'});continue
            p=active[-1];allocation={v:int(day) for day,vs in p['allocations'].items() for v in vs}
            if record.vin not in allocation or record.timestamp_min < w.calendar.day_to_start_min(allocation[record.vin]):
                failures.append({'world':index,'vin':record.vin,'time':record.timestamp_min,'plan':p['plan_id'],'error':'unauthorized or early W start'})
        audits.append({'world':index,'starts_checked':starts,'plans':w.review_plans})
    report={'execution_worlds':len(audits),'weld_starts_checked':sum(a['starts_checked'] for a in audits),'failures':failures,'audit':audits}
    Path(__file__).with_name('review-authorization-ledger.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='audit'},ensure_ascii=False))
    assert len(audits)==27 and not failures
if __name__=='__main__':main()
