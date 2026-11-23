def register(sites,run):
    allowed=set(sites);result={};m=run['matrix'];offset=run['offset']
    for p,code in run['records']:
        at=tuple(sum(a*b for a,b in zip(row,p))+shift for row,shift in zip(m,offset))
        if at not in allowed:raise ValueError('transformed observation outside declared sites')
        if at in result:raise ValueError('multiple observations at canonical die')
        result[at]={'source_at':list(p),'bin':code,'disposition':run['bins'][code]}
    return result
