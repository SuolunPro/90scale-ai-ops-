"""Use independent AI-estimated half-specific scoring intensities for a coherent joint distribution."""
import math
from collections import defaultdict

def states(h,a):
    def pmf(lam):
        v=[math.exp(-lam)]
        for n in range(1,15):v.append(v[-1]*lam/n)
        return [x/sum(v) for x in v]
    return [(i,j,x*y) for i,x in enumerate(pmf(h)) for j,y in enumerate(pmf(a))]
def direction(diff):return 0 if diff>0 else 1 if diff==0 else 2
def project(raw):
    rates=raw['half_rates']
    if len(rates)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) or not .01<=v<=3 for v in rates):
        raise ValueError('invalid half-specific intensities')
    ft=[0.]*3;rq=[0.]*3;hf=[0.]*9;g=[0.]*8;sc=defaultdict(float)
    first,second=states(*rates[:2]),states(*rates[2:])
    for h,a,p in first:
        half=direction(h-a)
        for x,y,q in second:
            i,j=h+x,a+y; mass=p*q;side=direction(i-j)
            ft[side]+=mass;rq[direction(i-j-1)]+=mass;hf[3*half+side]+=mass
            g[min(7,i+j)]+=mass;sc[i,j]+=mass
    if abs(rq[2]-ft[1]-ft[2])>1e-9 or any(abs(sum(hf[i::3])-ft[i])>1e-9 for i in range(3)):
        raise ValueError('cross-module identity')
    top=sorted(sc,key=lambda s:(-sc[s],sum(s),s))[:4]
    return dict(raw,spf=ft,rq=rq,htft=hf,goals=g,scores=[[f'{h}:{a}',sc[h,a]] for h,a in top])
