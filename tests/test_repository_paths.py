"""Lake refuses a repository path with a `.` component (invalid repository path), which failed T11
when an App was proposed at path `.`; the agent writes paths the way Lake stores them."""
from agent_application.agent import repository_path


def test_paths_under_the_app_root_have_no_dot_components() -> None:
    assert repository_path(".", "main.py") == "main.py"
    assert repository_path("", "./main.py") == "main.py"
    assert repository_path("./apps/health/", "./src/./app.py") == "apps/health/src/app.py"
    assert repository_path("apps/health", "requirements.txt") == "apps/health/requirements.txt"
    assert repository_path("/apps/health", "main.py") == "apps/health/main.py"
