"""The Titan payload cannot send source text to an arbitrary model override."""
import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest


def _configured(monkeypatch,model):
    if model is None:monkeypatch.delenv('EMBEDDING_MODEL',raising=False)
    else:monkeypatch.setenv('EMBEDDING_MODEL',model)
    path=Path(__file__).resolve().parents[1]/'src/embeddings.py'
    spec=importlib.util.spec_from_file_location('isolated_embedding_config',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('model',[None,'amazon.titan-embed-text-v2:0'])
def test_default_and_explicit_titan_use_supported_payload(monkeypatch,model):
    module=_configured(monkeypatch,model)
    vector=[1.0]+[0.0]*1023
    client=Mock();client.invoke_model.return_value={'body':io.BytesIO(json.dumps({'embedding':vector}).encode())}
    factory=Mock(return_value=client);monkeypatch.setattr(module,'_get_client',factory)
    assert module.generate_embedding('Synthetic source')==vector
    sent=client.invoke_model.call_args.kwargs
    assert sent['modelId']=='amazon.titan-embed-text-v2:0'
    assert json.loads(sent['body'])=={'inputText':'Synthetic source','dimensions':1024}


@pytest.mark.parametrize('model',['global.anthropic.claude-fable-5-1','cohere.embed-english-v3','custom-model','amazon.titan-embed-text-v2:0-extra',''])
def test_unsupported_embedding_override_denied_before_client(monkeypatch,model):
    module=_configured(monkeypatch,model)
    factory=Mock();monkeypatch.setattr(module,'_get_client',factory)
    with pytest.raises(ValueError,match='Unsupported embedding model'):
        module.generate_embedding('Synthetic private source')
    factory.assert_not_called()
