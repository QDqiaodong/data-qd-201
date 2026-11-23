from .checks import fields,array,integer,ident,unique

def coord(value):return tuple(integer(v,-10000,10000) for v in array(value,2,2))

def parse(data):
    fields(data,('wafer','sites','runs'));wafer=ident(data['wafer']);sites=[coord(v) for v in array(data['sites'],1,20000)];unique(sites)
    runs=[]
    for r in array(data['runs'],1,20):
        fields(r,('id','matrix','offset','bins','records'));key=ident(r['id'])
        matrix=[tuple(integer(v,-1,1) for v in array(row,2,2)) for row in array(r['matrix'],2,2)]
        if any(sum(v*v for v in row)!=1 for row in matrix) or sum(a*b for a,b in zip(*matrix))!=0:
            raise ValueError('signed orthogonal integer matrix required')
        offset=coord(r['offset']);bins={}
        fields(r['bins'],(),tuple(r['bins']) if isinstance(r['bins'],dict) else ())
        if not 1<=len(r['bins'])<=100:raise ValueError('bin table size')
        for code,state in r['bins'].items():
            ident(code)
            if state not in ('pass','fail','skip'):raise ValueError('invalid bin disposition')
            bins[code]=state
        records=[]
        for item in array(r['records'],0,20000):
            fields(item,('at','bin'));p=coord(item['at']);code=ident(item['bin'])
            if code not in bins:raise ValueError('unknown bin')
            records.append((p,code))
        unique([p for p,_ in records]);runs.append({'id':key,'matrix':matrix,'offset':offset,'bins':bins,'records':records})
    unique([r['id'] for r in runs])
    if sum(len(r['records']) for r in runs)>200000:raise ValueError('record work limit')
    return wafer,sites,runs
