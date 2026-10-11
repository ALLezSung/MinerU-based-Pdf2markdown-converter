"""Task isolation and offline end-to-end regression tests; no MinerU API calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from common import read, write, digest
from converter import inspect_sources, make_plan, plan_rows, run, validate_results, publish, package, review, locked
from hosted_worker import chunk_key
from settings import HERE, example, load_task


class ReusableTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=HERE)
        self.root=Path(self.temp.name)
        self.input=self.root/'inputs';self.input.mkdir()
        self.pdf=self.input/'sample.pdf'
        self.pdf_bytes(1)

    def tearDown(self):
        self.temp.cleanup()

    def pdf_bytes(self,count):
        from pypdf import PdfWriter
        writer=PdfWriter()
        for _ in range(count):writer.add_blank_page(width=200,height=200)
        writer.write(str(self.pdf))

    def task(self,name='case',profile='text_only',**overrides):
        folder=self.root/name;folder.mkdir(exist_ok=True)
        config=example(profile);config['task']={'id':name,'name':name}
        config['paths']['input_root']='../inputs'
        config['parse']['cache_dirs']=['../cache']
        config['review']['require_pilot']=False
        config['review']['require_manual_review']=False
        config['review']['require_issue_acknowledgement']=False
        config['review']['require_empty_page_review']=False
        config['validation']['math_syntax']=False
        for key,value in overrides.items():config[key]=value
        file=folder/'config.json';write(file,config)
        task=load_task(file)
        with locked(task):inspect_sources(task);make_plan(task)
        return task

    def seed(self,task,blocks=None,image=False):
        doc=read(task.state/'manifest.json')[0]
        request={'source_sha256':doc['sha256'],'hosted_model':'vlm'}
        chunk={'upload_pages':[1],'owned_pages':[1]}
        key=chunk_key(request,chunk)
        folder=self.root/'cache'/doc['id']/'chunks'/key;folder.mkdir(parents=True,exist_ok=True)
        archive=folder/'result.zip'
        if blocks is None:blocks=[{'type':'text','text':'完整正文：温度20℃，流量正常。','page_idx':0,'bbox':[100,200,900,300]}]
        with zipfile.ZipFile(archive,'w') as package:
            package.writestr('layout.json',json.dumps({'pdf_info':[{'page_idx':0,'page_size':[200,200]}]}))
            package.writestr('sample_content_list.json',json.dumps(blocks,ensure_ascii=False))
            if image:package.writestr('images/chart.png',b'asset-fixture')
        identity=hashlib.sha256(json.dumps({'source_sha256':doc['sha256'],'upload_pages':[1],'model':'vlm'},sort_keys=True).encode()).hexdigest()
        write(folder/'downloaded.json',{'identity':identity,'zip_sha256':digest(archive)})
        return doc

    def test_invalid_option_fails_before_side_effects(self):
        file=self.root/'bad.json';write(file,{'parse':{'chunck_pages':30}})
        with self.assertRaisesRegex(ValueError,'未知配置项'):load_task(file)
        self.assertFalse((self.root/'state').exists())

    def test_paths_resolve_from_config_not_working_directory(self):
        task=self.task()
        self.assertEqual(task.input_root,self.input.resolve())
        self.assertEqual(task.state,(self.root/'case/state').resolve())

    def test_two_tasks_do_not_share_state_or_outputs(self):
        a=self.task('one');b=self.task('two')
        self.seed(a)
        with locked(a):run(a,'body',execute=True,offline=True)
        self.assertTrue((a.state/'body').exists())
        self.assertFalse((b.state/'body').exists())
        self.assertNotEqual(a.output,b.output)

    def test_source_change_removes_old_corrections_and_reviews(self):
        task=self.task();payload=read(task.state/'plan.json');row=payload['documents'][0]
        row.update(body_pages='1',drop_blocks={'old':'reason'},correct_blocks={'old':{}},content_verified=True)
        write(task.state/'plan.json',payload)
        self.pdf_bytes(2)
        with locked(task):inspect_sources(task);make_plan(task)
        actual=plan_rows(task)[0]
        self.assertEqual(actual['drop_blocks'],{});self.assertEqual(actual['correct_blocks'],{})
        self.assertFalse(actual['content_verified']);self.assertEqual(actual['body_pages'],'')

    def test_rules_apply_only_to_matching_documents(self):
        config=example()['documents'];config['rules']=[{'match':'sample.pdf','set':{'chunk_pages':10,'pilot_pages':'1'}}]
        task=self.task(documents=config)
        self.assertEqual(plan_rows(task)[0]['chunk_pages'],10)

    def test_offline_complete_pipeline_and_arbitrary_file_count(self):
        task=self.task();doc=self.seed(task)
        with locked(task):
            run(task,'body',execute=True,offline=True);validate_results(task);publish(task);package(task)
        archive=task.delivery/'case_markdown.zip'
        self.assertTrue(archive.exists())
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(len([n for n in bundle.namelist() if n.startswith('Markdown/') and n.endswith('.md')]),1)
        self.assertFalse(plan_rows(task)[0]['content_verified'],'机器检查不能自动生成人工审核')

    def test_retain_images_copies_assets_and_packages_them(self):
        task=self.task(profile='with_images')
        blocks=[{'type':'text','text':'正文。','page_idx':0},
                {'type':'image','img_path':'images/chart.png','image_caption':['示意图'],'page_idx':0}]
        self.seed(task,blocks,image=True)
        with locked(task):run(task,'body',execute=True,offline=True);validate_results(task);publish(task);package(task)
        self.assertIn('assets/',(task.output/'sample.md').read_text(encoding='utf-8'))
        with zipfile.ZipFile(task.delivery/'case_markdown.zip') as bundle:
            self.assertTrue(any(name.endswith('chart.png') for name in bundle.namelist()))

    def test_preserve_and_drop_footer_are_configurable(self):
        task=self.task();config=read(task.config_path)
        config['cleaning']['drop_block_types'].remove('footer');write(task.config_path,config);task=load_task(task.config_path)
        self.seed(task,[{'type':'text','text':'正文','page_idx':0},{'type':'footer','text':'自定义页尾','page_idx':0}])
        # The renderer must support retaining a layout text type, not silently discard it.
        with locked(task):run(task,'body',execute=True,offline=True)
        quality=read(next((task.state/'body').glob('*/quality.json')))
        self.assertFalse(any(i['code']=='unrenderable_block' for i in quality['issues']))
        self.assertIn('自定义页尾',next((task.state/'body').glob('*/body.md')).read_text(encoding='utf-8'))

    def test_manual_gate_and_explicit_approval_binding(self):
        task=self.task();config=read(task.config_path);config['review']['require_manual_review']=True
        write(task.config_path,config);task=load_task(task.config_path)
        doc=self.seed(task)
        with locked(task):
            run(task,'body',execute=True,offline=True);validate_results(task)
            with self.assertRaisesRegex(ValueError,'人工审核'):publish(task)
            review(task,[doc['id']],True);publish(task)
        self.assertEqual(plan_rows(task)[0]['reviewed_markdown_sha256'],digest(task.output/'sample.md'))
        rows=plan_rows(task);rows[0]['content_verified']=False
        write(task.state/'plan.json',{'schema_version':1,'task_id':'case','documents':rows})
        with locked(task),self.assertRaisesRegex(ValueError,'人工审核'):package(task)

    def test_packaging_rejects_modified_published_image(self):
        task=self.task(profile='with_images')
        self.seed(task,[{'type':'image','img_path':'images/chart.png','image_caption':['图'],'page_idx':0}],image=True)
        with locked(task):run(task,'body',execute=True,offline=True);validate_results(task);publish(task)
        next(task.output.rglob('*.png')).write_bytes(b'changed')
        with locked(task),self.assertRaisesRegex(ValueError,'资产被修改'):package(task)

    def test_missing_cache_offline_does_not_generate_completion(self):
        task=self.task()
        with locked(task),self.assertRaises(ValueError):run(task,'body',execute=True,offline=True)
        self.assertFalse(list((task.state/'body').glob('*/completed.json')))

    def test_signature_invalidates_after_cleaning_policy_change(self):
        task=self.task();self.seed(task)
        with locked(task):run(task,'body',execute=True,offline=True);validate_results(task)
        config=read(task.config_path);config['cleaning']['paragraph_join']['enabled']=False
        write(task.config_path,config);task=load_task(task.config_path)
        with locked(task),self.assertRaisesRegex(ValueError,'验证未通过'):validate_results(task)

    def test_wrong_task_cannot_claim_state_directory(self):
        task=self.task();config=read(task.config_path);config['task']['id']='other'
        write(task.config_path,config);changed=load_task(task.config_path)
        with self.assertRaisesRegex(ValueError,'另一个任务'):
            with locked(changed):pass

    def test_pilot_gate_is_configurable_and_not_silently_bypassed(self):
        task=self.task();config=read(task.config_path);config['review']['require_pilot']=True
        write(task.config_path,config);task=load_task(task.config_path)
        with self.assertRaisesRegex(ValueError,'小样尚未验收'):run(task,'body')


if __name__=='__main__':unittest.main()
