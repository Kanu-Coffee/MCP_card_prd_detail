import json
import subprocess
from pathlib import Path
import httpx

config=json.loads(subprocess.check_output(['/opt/cardrag/009-b544a80/operations/mcp-compose.sh','config','--format','json']))
secret=Path(config['secrets']['mcp_bearer_token']['file'])
headers={'Authorization':'Bearer '+secret.read_text().strip(),'Accept':'application/json, text/event-stream'}
client=httpx.Client(base_url='http://127.0.0.1:18015',headers=headers,timeout=90)
counter=0
def rpc(method,params):
 global counter
 counter+=1
 response=client.post('/mcp',json={'jsonrpc':'2.0','id':counter,'method':method,'params':params})
 response.raise_for_status()
 if 'mcp-session-id' in response.headers:
  client.headers['mcp-session-id']=response.headers['mcp-session-id']
 if response.headers.get('content-type','').startswith('text/event-stream'):
  events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
  payload=next(e for e in events if e.get('id')==counter)
 else:
  payload=response.json()
 assert 'error' not in payload, 'MCP RPC returned error'
 result=payload['result']
 assert not result.get('isError',False), 'MCP tool returned error'
 return result
rpc('initialize',{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'010-operational-smoke','version':'1'}})
products=[('woori','500107'),('woori','104022'),('woori','104023'),('hana','15911'),('shinhan','00368')]
results=[]
for issuer,code in products:
 summary=rpc('tools/call',{'name':'get_product_summary','arguments':{'issuer':issuer,'identifier':code}})['structuredContent']
 assert summary and summary['product_code']==code
 assert not any('부가서비스 변경' in text or '상품 출시일' in text for text in summary['benefit_headings'])
 if code=='500107':
  assert any('1.2%' in t for t in summary['benefit_summary_texts'])
  assert '15,000' in summary['annual_fee_text']
  assert not any('조건, 한도 없이 든든한 1.2% 할인' in t for t in summary['condition_summary_texts'])
 bundle=rpc('tools/call',{'name':'get_contract_bundle','arguments':{'contract_revision_id':summary['contract_revision_id'],'scope':'benefits','include_links':True}})
 data=bundle.get('structuredContent')
 if data is None:
  data=[block.get('text','') for block in bundle.get('content',[]) if block.get('type')=='text']
 serialized=json.dumps(data,ensure_ascii=False)
 if code=='500107':assert '1.2%' in serialized
 results.append({'summary':summary,'bundle':data,'http_authenticated':True})
Path('/home/lee/projects/MCP_card_prd_detail/.handoff/010_009-production-cutover/evidence/operational-smoke.json').write_text(json.dumps({'method':'Authenticated MCP HTTP summary and same-revision benefit bundle, no OCR/embedding calls','products':results},ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'products_checked':len(results),'generation_ids':sorted({r['summary']['generation_id'] for r in results}),'passed':True}))
