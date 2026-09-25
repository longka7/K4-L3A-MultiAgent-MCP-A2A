import sys
import pytest

if __name__ == "__main__":
    code = pytest.main([
        "-v",
        "tests/test_workflow.py",
        "tests/test_order_item.py",
        "tests/test_payment_agent.py",
        "tests/test_policy_verifier.py",
    ])
    sys.exit(code)
