"""Reassemble one text-only MaaS SSE response without exposing partial output.

The private evidence retains exact final/reasoning channels, usage, a hash of
received data events and timing. No token stream is printed or acted upon.
"""
from copy import deepcopy
import hashlib
import json
import time

import requests


class StreamProtocolError(ValueError):
    pass


def receive(response, *, model, started, max_duration, record, clock=time.monotonic):
    content=[];reasoning=[];usage=None;identifier=None;created=None;finish=None
    metadata={'version':'maas_sse_text_v1','data_events':0,'done_received':False,
              'first_event_seconds':None,'first_content_seconds':None,
              'max_duration_seconds':max_duration,'partial_content_used':False}
    record['stream_observation']=metadata
    event_hash=hashlib.sha256();pending=[];done=False

    def envelope():
        return {'id':identifier,'object':'chat.completion','created':created,'model':model,
            'choices':[{'index':0,'finish_reason':finish,'message':{
                'role':'assistant','content':''.join(content),'reasoning_content':''.join(reasoning)}}],
            'usage':deepcopy(usage)}

    def consume(payload):
        nonlocal usage,identifier,created,finish,done
        metadata['data_events']+=1
        event_hash.update(len(payload).to_bytes(8,'big'));event_hash.update(payload)
        if metadata['first_event_seconds'] is None:metadata['first_event_seconds']=clock()-started
        if payload.strip()==b'[DONE]':
            done=True;metadata['done_received']=True;return
        try:chunk=json.loads(payload)
        except (ValueError,UnicodeDecodeError):raise StreamProtocolError('invalid_sse_json') from None
        if not isinstance(chunk,dict) or chunk.get('error') is not None:
            raise StreamProtocolError('provider_sse_error')
        if chunk.get('model') != model:raise StreamProtocolError('stream_model_mismatch')
        if identifier is None:identifier=chunk.get('id');created=chunk.get('created')
        if not identifier or chunk.get('id')!=identifier:raise StreamProtocolError('stream_identity_changed')
        if chunk.get('usage') is not None:
            if usage is not None and usage!=chunk['usage']:raise StreamProtocolError('conflicting_usage_chunks')
            usage=deepcopy(chunk['usage'])
        choices=chunk.get('choices')
        if not isinstance(choices,list) or len(choices)>1:raise StreamProtocolError('unexpected_stream_choices')
        if not choices:return
        choice=choices[0]
        if choice.get('index')!=0:raise StreamProtocolError('unexpected_stream_choice_index')
        delta=choice.get('delta') or {}
        if not isinstance(delta,dict) or delta.get('tool_calls') or delta.get('function_call'):
            raise StreamProtocolError('nontext_stream_output')
        if delta.get('role') not in (None,'assistant'):raise StreamProtocolError('unexpected_stream_role')
        for key,target in (('content',content),('reasoning_content',reasoning)):
            value=delta.get(key)
            if value is not None and not isinstance(value,str):raise StreamProtocolError('nontext_stream_delta')
            if value:
                if finish is not None:raise StreamProtocolError('content_after_finish')
                target.append(value)
                if key=='content' and metadata['first_content_seconds'] is None:
                    metadata['first_content_seconds']=clock()-started
        reason=choice.get('finish_reason')
        if reason is not None:
            if finish is not None and finish!=reason:raise StreamProtocolError('conflicting_finish_reason')
            finish=reason

    try:
        for line in response.iter_lines(chunk_size=1):
            if clock()-started>max_duration:
                record['failure_reason_code']='stream_wall_deadline'
                raise requests.ReadTimeout('stream_wall_deadline')
            if not isinstance(line,bytes):raise StreamProtocolError('nonbyte_sse_line')
            if not line:
                if pending:consume(b'\n'.join(pending));pending=[]
                if done:break
            elif line.startswith(b'data:'):
                pending.append(line[5:].lstrip(b' '))
            elif line.startswith((b':',b'event:',b'id:',b'retry:')):continue
            else:raise StreamProtocolError('unrecognized_sse_field')
        if pending and not done:consume(b'\n'.join(pending))
        if not done or finish not in {'stop','length'}:
            raise StreamProtocolError('missing_stream_completion')
        return envelope()
    except BaseException:
        record['partial_response_body']=envelope()
        raise
    finally:
        metadata['received_data_sha256']=event_hash.hexdigest()
        metadata['final_content_characters']=sum(map(len,content))
        metadata['private_reasoning_characters']=sum(map(len,reasoning))
        response.close()
