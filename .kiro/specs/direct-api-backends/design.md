# Provider request and accounting: design

Role resolution supplies the invoker and role specification. DirectApiInvoker captures usage before parsing and returns the shared InvocationResult; BedrockInvoker owns Converse payload and response translation.

invoke_metered checks the threshold before a call, charges known failed responses and blocks continuation when usage cannot be priced. Provider, output-contract and routing failures remain distinguishable to consumers.

Code authority: [src/backends/direct_api.py](../../../src/backends/direct_api.py), [src/backends/bedrock.py](../../../src/backends/bedrock.py), [src/backends/base.py](../../../src/backends/base.py), [src/backends/cost.py](../../../src/backends/cost.py).

Regression authority: [tests/test_bedrock.py](../../../tests/test_bedrock.py), [tests/test_cost.py](../../../tests/test_cost.py), [tests/test_metered_stages.py](../../../tests/test_metered_stages.py). Test presence is not a passing result.
