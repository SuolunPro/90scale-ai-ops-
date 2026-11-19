"""One isolated, prospective source-grounded research observation."""
import base64
import datetime as dt
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import urllib.request

UTC = dt.timezone.utc
BJT = dt.timezone(dt.timedelta(hours=8))
DIRECTIONS = ('胜','平','负')
HANDICAP = ('让胜','让平','让负')
HALVES = tuple(h+'-'+f for h in DIRECTIONS for f in DIRECTIONS)
GOALS = tuple(map(str,range(7)))+('7+',)
FACTS = {
    'sale_day':'2026-11-19','match_no':'003','home':'奥兰多城','away':'辛辛那提FC',
    'kickoff_bjt':'2026-11-20 08:30','handicap':-1,'observation_window_bjt':'2026-11-19 上午，最迟12:01已存档',
    'sources':{
        'LOTA':{'url':'https://hc.lota.tv/#/main','path':'竞足→周四003→分析/指数/必发',
                'fixture_sp':[2.34,3.51,2.34],'handicap_sp':[4.65,4.35,1.44],
                'expected_goals':[1.85,1.31],'macau_asian':['0.87 平手 0.91','0.88 平手 0.90'],
                'exchange_volumes_wan':[0.15,0.21,0.05],'pl_index':[-8,119,-68]},
        'Okooo':{'url':'https://www.okooo.com/soccer/match/1350991/exchanges/',
                 'exchange_price':[2.52,4.30,2.66],'exchange_volume':[1502,1990,495],
                 'shares_pct':[37.67,49.91,12.42],'hot_cold':[-3,112,-67],'pl_index':[5,-115,67]}},
    'actual_publication_times':None,'absences_and_starting_lineups':None}
SCHEMA = {'spf':'3 numbers home draw away','rq':'3 numbers handicap home draw away',
          'scores':'4 distinct [score,probability] ordered, use colon scores; absolute probabilities',
          'htft':'9 numbers in order 胜-胜 胜-平 胜-负 平-胜 平-平 平-负 负-胜 负-平 负-负',
          'goals':'8 numbers 0,1,2,3,4,5,6,7+','risk':'WATCH or ALERT or CLEAR',
          'reason':'short Chinese risk reason','single':'PASS or 胜 平 负 让胜 让平 让负',
          'single_reason':'short Chinese reason','facts_used':'list of source names'}
def api(path, body=None):
    req=urllib.request.Request('https://api.deepseek.com/'+path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+os.environ['DEEPSEEK_API_KEY'],'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=80) as response:
        return json.load(response)
def balance():
    return next(Decimal(v['total_balance']) for v in api('user/balance')['balance_infos'] if v['currency']=='CNY')
def probabilities(values,keys):
    if len(values)!=len(keys) or any(type(v) not in (int,float) or not math.isfinite(v) or v<0 or v>1 for v in values) or abs(sum(values)-1)>.015:
        raise ValueError('invalid distribution')
    return dict(zip(keys,(v/sum(values) for v in values)))
def rank(values,n):
    return sorted(values,key=values.get,reverse=True)[:n]
def canonical(raw):
    import re
    ft=probabilities(raw['spf'],DIRECTIONS)
    rq=probabilities(raw['rq'],HANDICAP)
    hf=probabilities(raw['htft'],HALVES)
    g=probabilities(raw['goals'],GOALS)
    scores=dict(raw['scores'])
    if len(raw['scores'])!=4 or len(scores)!=4 or any(not re.fullmatch(r'\d+:\d+',s) or type(p) not in (int,float) or not 0<p<1 for s,p in scores.items()) or sum(scores.values())>1:
        raise ValueError('invalid scores')
    if raw['risk'] not in ('CLEAR','WATCH','ALERT') or raw['single'] not in (*DIRECTIONS,*HANDICAP,'PASS'):
        raise ValueError('invalid choice')
    if set(raw['facts_used'])!={'LOTA','Okooo'}:raise ValueError('unrecognized evidence')
    r=rank(rq,2)
    if set(r)=={'让胜','让负'}:r[1]='让平'
    over=sum(g[k] for k in GOALS[3:])
    return {'胜平负':{'probabilities':ft,'top1':rank(ft,1)[0],'top2':rank(ft,2)},
            '让球':{'handicap':-1,'probabilities':rq,'top2':r},
            '比分':{'top3':list(scores)[:3],'top4':list(scores),'probabilities':scores},
            '半全场':{'top3':rank(hf,3),'probabilities':hf},
            '总进球':{'top3':[int(k) if k!='7+' else k for k in rank(g,3)],'probabilities':g},
            '大小球研判':{'line':2.5,'over':over,'under':1-over,'source':'AI总进球分布','market_ou_line':None},
            '冷门预警':{'type':'AI_INDEPENDENT','status':raw['risk'],'reason':raw['reason']},
            '精选单选':{'direction':raw['single'],'reason':raw['single_reason'],'type':'AI_SUGGESTION'}}
def now():
    return dt.datetime.now(UTC)
def run():
    end=dt.datetime(2026,11,19,22,tzinfo=BJT)
    messages=[{'role':'system','content':'Independent soccer forecast. Use only supplied timestamped facts. No other predictions, external tools or invented news. Return exactly one flat JSON object at the root with these exact English keys: spf, rq, scores, htft, goals, risk, reason, single, single_reason, facts_used. Do not nest it inside output, facts, analysis or prediction. Use native numeric arrays and exact key spellings. Each full distribution must sum to 1. Interpret sources separately; different profit indices need not agree. Asian line is not official handicap. Choose PASS if inputs cannot justify a single. Use source names LOTA and Okooo.'},
              {'role':'user','content':json.dumps({'facts':FACTS,'output':SCHEMA},ensure_ascii=False)}]
    limit=1800
    input_ceiling=len(json.dumps(messages,ensure_ascii=False).encode())+256
    ceiling=Decimal(input_ceiling*2+limit*8)/1000000
    if now()>=end or ceiling>Decimal('0.10') or balance()<ceiling:raise ValueError('preflight guard')
    before=balance()
    res=api('chat/completions',{'model':'deepseek-flash','thinking':{'type':'disabled'},'max_tokens':limit,'messages':messages,'response_format':{'type':'json_object'}})
    frozen=now()
    usage=res['usage']
    after=balance()
    receipt={'id':res['id'],'model':res['model'],'usage':usage,'observed_delta_cny':str(before-after),'max_cny':str(ceiling),'received_at':frozen.isoformat()}
    print('RECEIPT '+json.dumps(receipt),flush=True)
    if frozen>=end:raise ValueError('late response')
    raw=json.loads(res['choices'][0]['message']['content'])
    result={'schema':'tricore-shadow-v1','core':'DeepSeek','fixture':FACTS,'model':res['model'],
            'frozen_at':frozen.isoformat(),'fact_hash':hashlib.sha256(json.dumps(FACTS,sort_keys=True).encode()).hexdigest(),
            'raw':raw,'receipt':receipt,'settlement':'PENDING','formal_comparison':'WITHHELD_FROM_PROMPT'}
    try:
        result['modules']=canonical(raw)
        result['validation']='VALID'
    except (TypeError,KeyError,ValueError) as error:
        result['validation']=type(error).__name__
    encoded=json.dumps(result,ensure_ascii=False).encode()
    # Only the research recipient can read predictions from the public runner log.
    key=os.urandom(32);iv=os.urandom(16)
    wrapped=subprocess.run(['openssl','pkeyutl','-encrypt','-pubin','-inkey','research/phase2/recipient.pem','-pkeyopt','rsa_padding_mode:oaep','-pkeyopt','rsa_oaep_md:sha256'],input=key,check=True,capture_output=True).stdout
    encrypted=subprocess.run(['openssl','enc','-aes-256-cbc','-K',key.hex(),'-iv',iv.hex()],input=encoded,capture_output=True,check=True).stdout
    print('SEALED '+json.dumps([base64.b64encode(x).decode() for x in (wrapped,iv,encrypted)]))
    print('DIGEST '+hashlib.sha256(encoded).hexdigest())
if __name__=='__main__':run()
