import io
import json
import unittest
from unittest.mock import patch
from stock_research import models
from stock_research.core import ServiceError
from stock_research.model_endpoints import endpoint_settings,api_url,check_custom_destination


class EndpointTests(unittest.TestCase):
    def test_both_deepseek_protocol_urls(self):
        for protocol,base,url in [('openai','https://api.deepseek.com','https://api.deepseek.com/chat/completions'),('anthropic','https://api.deepseek.com/anthropic','https://api.deepseek.com/anthropic/v1/messages')]:
            e={**endpoint_settings('deepseek',protocol,base),'provider':'deepseek'}
            self.assertEqual(api_url(e,'completion'),url)
            self.assertEqual(api_url(e,'models'),'https://api.deepseek.com/models')
    def test_bad_urls_rejected_before_authentication(self):
        for base in ('http://api.deepseek.com','https://user:password@example.com','https://127.0.0.1','https://localhost','https://api.deepseek.com/chat/completions','https://api.deepseek.com?key=hidden'):
            with self.assertRaises(ServiceError):endpoint_settings('deepseek','openai',base)
    def test_custom_destinations_require_consent_and_public_dns(self):
        e={**endpoint_settings('deepseek','openai','https://custom.example/v1'),'provider':'deepseek'}
        with self.assertRaises(ServiceError):check_custom_destination(e)
        e['base_url_confirmed']=True
        with patch('stock_research.model_endpoints.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(ServiceError):check_custom_destination(e)
    def test_anthropic_auth_body_and_structured_tool_result(self):
        cfg={'provider':'deepseek','model':'deepseek-v4-pro','single_model':True,'api_protocol':'anthropic','base_url':'https://api.deepseek.com/anthropic','max_output_tokens':1000,'prefer_off_peak':False}
        response={'stop_reason':'tool_use','content':[{'type':'tool_use','name':'submit_report','input':{'ok':True}}],'usage':{'input_tokens':1,'output_tokens':2}}
        schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
        with patch.object(models,'api_key',return_value='not-a-real-credential'),patch.object(models,'model_open',return_value=io.BytesIO(json.dumps(response).encode())) as call:
            result,meta=models.completion('instruction','prompt',schema,settings=cfg,purpose='financial_extraction')
            request=call.call_args.args[0]
            self.assertEqual(request.full_url,'https://api.deepseek.com/anthropic/v1/messages')
            self.assertIn('X-api-key',dict(request.header_items()))
            self.assertNotIn('Authorization',dict(request.header_items()))
            body=json.loads(request.data)
            self.assertEqual(body['thinking'],{'type':'disabled'})
            self.assertEqual(body['tool_choice']['name'],'submit_report')
            self.assertEqual(result,{'ok':True})
            self.assertEqual(meta['api_protocol'],'anthropic')
    def test_redirects_never_forward_custom_credentials(self):
        with self.assertRaises(ServiceError):models.NoRedirect().redirect_request(None,None,302,'',{},'https://other.example')

    def test_official_and_custom_transports_both_install_the_redirect_blocker(self):
        request=models.urllib.request.Request('https://api.deepseek.com/models',headers={'Authorization':'Bearer test-only'})
        for custom in (False,True):
            with patch.object(models.urllib.request,'build_opener') as build,patch.object(models.urllib.request,'urlopen') as unsafe:
                models.model_open(request,timeout=3,custom=custom)
                self.assertIsInstance(build.call_args.args[0],models.NoRedirect)
                build.return_value.open.assert_called_once_with(request,timeout=3)
                unsafe.assert_not_called()
        for code in (301,302,303,307,308):
            with self.assertRaises(ServiceError) as caught:
                models.NoRedirect().redirect_request(request,None,code,'',{},'https://other.example/endpoint')
            self.assertEqual(caught.exception.code,'unsafe_model_redirect')


if __name__=='__main__':unittest.main()
