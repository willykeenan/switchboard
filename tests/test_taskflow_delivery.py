"""Missing canonical intake-suite fixture restored using actual delivery services.
No provider, live board, install or production destination is used.
"""
import json, unittest
from pathlib import Path
import test_taskflow as fixtures

class DeliveryTests(unittest.TestCase):
    tearDown=fixtures.TaskFlowTests.tearDown
    worker=fixtures.TaskFlowTests.worker
    def setUp(self):
        fixtures.TaskFlowTests.setUp(self)
        self.contract={'kind':'document','destination':{'kind':'directory','path':str(self.root/'delivered')},'actorId':'codex:b','criteria':[{'id':'contents','description':'Exact destination contents'}]}
    def review(self,task):
        self.store.audit.claim(task['taskId'],'codex:audit')
        self.store.audit.prepare(task['taskId'],'codex:audit',[],'Synthetic exact task and original returned output examined; no Library context required.')
        evidence=self.root/'findings.md';evidence.write_text('Fixture independent findings')
        return self.store.review(task['taskId'],self.store.get(task['taskId'])['version'],'codex:audit','accept','Exact fixture output verified',[str(evidence)])
    def receipt(self,task):
        destination=self.root/'delivered';destination.mkdir(exist_ok=True)
        outputs=[]
        for i,f in enumerate(task['result']['artifacts']):
            path=destination/('output-'+str(i)+'.md');path.write_bytes(Path(f['path']).read_bytes());outputs.append(str(path))
        receipt=self.root/'delivery-receipt.json'
        receipt.write_text(json.dumps({'taskId':task['taskId'],'approvalId':task['approval']['id'],'pins':task['approval']['pins'],'destination':task['delivery']['destination'],'outputs':outputs}))
        evidence=self.root/'delivery-evidence.md';evidence.write_text('Exact fixture destination bytes copied and checked')
        return {'receipt':str(receipt),'evidence':[str(evidence)],'sourceReleased':True}
