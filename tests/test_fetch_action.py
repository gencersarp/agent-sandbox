import pytest
from pathlib import Path
from src.agent import AgentRunner, RepoContext, LLMConfig
from src.manifest import Manifest
from src.sandbox import SandboxViolationError

def test_fetch_action_success(tmp_path, monkeypatch):
    """Test that the agent can successfully fetch a URL allowed by the manifest."""
    manifest_data = {
        "agent_task": {"description": "test fetch"},
        "network": {"allowed_domains": ["example.com"]}
    }
    manifest = Manifest(**manifest_data)
    ctx = RepoContext(root=tmp_path)
    llm_config = LLMConfig(api_key="fake")
    runner = AgentRunner(manifest, ctx, llm_config)

    # Mock LLM response to perform a fetch
    def mock_chat(*args, **kwargs):
        return '[{"action": "fetch", "url": "https://example.com/data.json"}]'
    monkeypatch.setattr(runner.llm, "chat", mock_chat)

    # Mock httpx.get to return a fake response
    class MockResponse:
        def __init__(self, text, status_code=200):
            self.text = text
            self.status_code = status_code
        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx
                raise httpx.HTTPStatusError("error", request=None, response=self)

    import httpx
    def mock_get(self, url, **kwargs):
        if "example.com" in url:
            return MockResponse('{"hello": "world"}')
        return MockResponse("Not Found", 404)
    
    monkeypatch.setattr(httpx.Client, "get", mock_get)

    report = runner.run()
    
    # Check if fetch was logged in some way or if we can verify it happened.
    # For now, let's just ensure it doesn't crash and we can extend it.
    assert not report.errors
    # We might want to store fetch results in the runner so the agent can read them in next steps,
    # or just return them to the LLM if we had a multi-turn agent.
    # For MVP, fetch might just be to check if something exists or to get data for a write.

def test_fetch_action_denied(tmp_path, monkeypatch):
    """Test that fetching a URL NOT allowed by the manifest raises an error."""
    manifest_data = {
        "agent_task": {"description": "test fetch"},
        "network": {"allowed_domains": ["example.com"]}
    }
    manifest = Manifest(**manifest_data)
    ctx = RepoContext(root=tmp_path)
    llm_config = LLMConfig(api_key="fake")
    runner = AgentRunner(manifest, ctx, llm_config)

    # Mock LLM response to perform a fetch to a disallowed domain
    def mock_chat(*args, **kwargs):
        return '[{"action": "fetch", "url": "https://malicious.com"}]'
    monkeypatch.setattr(runner.llm, "chat", mock_chat)

    report = runner.run()
    
    assert any("Network access denied" in err for err in report.errors)
