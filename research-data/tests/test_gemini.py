"""Google's OpenAI-compatible contract, isolated credentials and routing."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from stock_research import core,models
from stock_research.core import ServiceError
from stock_research.model_endpoints import endpoint_settings,api_url,check_custom_destination

BASE='https://generativelanguage.googleapis.com/v1beta/openai'
SCHEMA={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}

class GeminiTests(unittest.TestCase):
    def settings(self,model='gemini-3.8-flash'):
        return {'provider':'gemini','model':model,'single_model':True,'max_output_tokens':10000,'api_protocol':'openai','base_url':BASE}

    def test_official_urls_protocol_and_destination(self):
        ep={**endpoint_settings('gemini'),'provider':'gemini'}
        self.assertFalse(ep['custom_base_url'])
        self.assertEqual(api_url(ep,'completion'),BASE+'/chat/completions')
        self.assertEqual(api_url(ep,'models'),BASE+'/models')
        check_custom_destination(ep)
        for protocol,base in [('anthropic',BASE),('openai',BASE+'/v1'),('openai',BASE+'/chat/completions')]:
            with self.assertRaises(ServiceError):endpoint_settings('gemini',protocol,base)
        with self.assertRaises(ServiceError):check_custom_destination({**endpoint_settings('gemini','openai','https://custom.example/v1'),'provider':'gemini'})

    def test_json_schema_auth_usage_and_actual_model(self):
        response={'model':'gemini-3.8-flash','choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}],'usage':{'prompt_tokens':123,'completion_tokens':45}}
        with patch.object(models,'api_key',return_value='fake-google-key-only'),patch.object(models,'model_open',return_value=io.BytesIO(json.dumps(response).encode())) as net:
            result,meta=models.completion('instruction','prompt',SCHEMA,settings=self.settings(),purpose='financial_extraction')
        req=net.call_args.args[0];body=json.loads(req.data)
        self.assertEqual(req.full_url,BASE+'/chat/completions')
        self.assertEqual(req.get_header('Authorization'),'Bearer fake-google-key-only')
        self.assertNotIn('X-api-key',dict(req.header_items()))
        self.assertEqual(body['response_format']['json_schema']['schema'],SCHEMA)
        self.assertEqual(body['response_format']['type'],'json_schema')
        self.assertEqual(body['messages'][0]['content'],'instruction')
        self.assertEqual(body['max_tokens'],10000)
        self.assertEqual(body['reasoning_effort'],'low')
        self.assertNotIn('thinking',body)
        self.assertEqual(result,{'ok':True})
        self.assertEqual(meta['response_model'],'gemini-3.8-flash')
        self.assertEqual(meta['usage']['prompt_tokens'],123)

    def test_catalog_normalizes_deduplicates_and_disables_non_text_models(self):
        response={'data':[{'id':'models/gemini-3.8-flash'},{'id':'gemini-3.8-flash'},{'id':'gemini-3-pro-image-preview'},{'id':'gemini-2.5-flash-preview-tts'},{'id':'veo-3.1-generate-preview'},{'id':'gemini-embedding-001'}]}
        with patch.object(models,'api_key',return_value='fake-google-key-only'),patch.object(models,'model_open',return_value=io.BytesIO(json.dumps(response).encode())) as net:
            catalog=models.list_models('gemini')
        self.assertEqual(net.call_args.args[0].full_url,BASE+'/models')
        self.assertEqual(len(catalog['models']),5)
        self.assertEqual([m['id'] for m in catalog['models'] if m['research_compatible']],['gemini-3.8-flash'])

    def test_truncated_gemini_output_is_not_a_completed_report(self):
        response={'choices':[{'finish_reason':'length','message':{'content':'{"ok":'}}]}
        with patch.object(models,'api_key',return_value='fake-google-key-only'),patch.object(models,'model_open',return_value=io.BytesIO(json.dumps(response).encode())):
            with self.assertRaises(ServiceError) as caught:models.completion('instruction','prompt',SCHEMA,settings=self.settings())
        self.assertEqual(caught.exception.code,'model_output_incomplete')

    def test_missing_key_stops_before_network_and_old_models_skip_reasoning(self):
        with patch.object(models,'api_key',side_effect=ServiceError('missing_key','not configured')),patch.object(models,'model_open') as net:
            with self.assertRaises(ServiceError):models.completion('instruction','prompt',settings=self.settings())
            net.assert_not_called()
        response={'choices':[{'finish_reason':'stop','message':{'content':'OK'}}]}
        for model,budget in [('gemini-2.0-flash',256),('gemini-2.5-pro',2048),('gemini-3.8-flash',2048)]:
            with patch.object(models,'api_key',return_value='fake-google-key-only'),patch.object(models,'model_open',return_value=io.BytesIO(json.dumps(response).encode())) as net:
                models.completion('instruction','prompt',settings=self.settings(model),purpose='connection_test',max_tokens=256)
            body=json.loads(net.call_args.args[0].data)
            self.assertEqual(body['max_tokens'],budget)
            self.assertEqual('reasoning_effort' in body,model!='gemini-2.0-flash')

    def test_profile_save_secret_isolation_and_stage_fallback(self):
        cfg=core.config();cfg['model_profiles']=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'automation.json').write_text(json.dumps(cfg))
            with patch.object(core,'ROOT',root),patch.object(core,'PROJECT',root),patch.dict('os.environ',{},clear=True):
                saved=core.save_settings({'provider':'gemini','model':'gemini-3.8-flash','api_key':'fake-google-key-not-real','stages':['analysis']})
                g=saved['profiles'][0]
                self.assertTrue(saved['configured']['gemini'])
                self.assertEqual(g['base_url'],BASE)
                self.assertEqual(g['stages'],['deep_analysis'])
                self.assertNotIn('fake-google',json.dumps(saved))
                self.assertEqual(core.api_key('gemini',g['id']),'fake-google-key-not-real')
                self.assertEqual((root/'.env.local').stat().st_mode&0o777,0o600)
                core.save_settings({'provider':'deepseek','model':'deepseek-v4-pro','api_key':'fake-deepseek-key-not-real','stages':['extraction','analysis']})
                self.assertEqual(core.api_key('gemini',g['id']),'fake-google-key-not-real')
                with patch.object(models,'_completion_once',return_value=('ok',{})) as call:
                    models.completion('','',purpose='financial_extraction',settings=core.config())
                    self.assertEqual(call.call_args.args[-1]['provider'],'deepseek')
                with patch.object(models,'_completion_once',side_effect=[ServiceError('rate_limited','temporary'),('ok',{})]) as call:
                    models.completion('','',purpose='comprehensive_report',settings=core.config())
                    self.assertEqual([c.args[-1]['provider'] for c in call.call_args_list],['gemini','deepseek'])

    def test_legacy_gemini_env_reference_is_accepted(self):
        cfg={**core.config(),'model_profiles':[{'id':'g','provider':'gemini','model':'gemini-3.8-flash','enabled':False,'test_mode':False,'key_env':'GEMINI_API_KEY'}]}
        with patch.object(core,'config',return_value=cfg),patch.dict('os.environ',{'GEMINI_API_KEY':'fake-google-environment-key'}):
            self.assertEqual(core.api_key('gemini','g'),'fake-google-environment-key')

if __name__=='__main__':unittest.main()
