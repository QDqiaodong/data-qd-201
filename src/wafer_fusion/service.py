from .model import parse
from .register import register

def process(data):
    wafer,sites,runs=parse(data);registered=[register(sites,r) for r in runs];dies=[];pick=[]
    for at in sorted(sites,key=lambda p:(p[1],p[0])):
        observations=[];reasons=[]
        for run,observed in zip(runs,registered):
            observation=observed.get(at)
            if observation is None:
                observations.append({'run':run['id'],'disposition':'missing','bin':None,'source_at':None})
                reasons.append({'run':run['id'],'reason':'missing'})
            else:
                observations.append({'run':run['id'],**observation})
                if observation['disposition']!='pass':reasons.append({'run':run['id'],'reason':observation['disposition']})
        dispositions={o['disposition'] for o in observations}
        status='fail' if 'fail' in dispositions else ('incomplete' if reasons else 'pass')
        if status=='pass':pick.append(list(at))
        dies.append({'die_id':f'{wafer}:{at[0]}:{at[1]}','at':list(at),'status':status,'observations':observations,'blocked_by':reasons})
    return {'wafer':wafer,'dies':dies,'pick_coordinates':pick,'summary':{'sites':len(sites),'runs':len(runs),'pickable':len(pick),'failed':sum(d['status']=='fail' for d in dies),'incomplete':sum(d['status']=='incomplete' for d in dies)}}
