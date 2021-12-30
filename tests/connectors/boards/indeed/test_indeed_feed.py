import os
import json
import pytest
from hrflow import Hrflow

from hrflow_connectors.connectors.boards.indeed.actions import IndeedFeed

# Adding web driver manager as a DEV dependency to make testing easier for users
from webdriver_manager.chrome import ChromeDriverManager


@pytest.fixture
def credentials(pytestconfig):
    with open(os.path.join(pytestconfig.rootpath, "credentials.json"), "r") as f:
        credentials = json.loads(f.read())
    return credentials


@pytest.fixture
def hrflow_client(credentials):
    x_api_key = credentials["hrflow"]["x-api-key"]
    x_user_email = credentials["hrflow"]["x-user-email"]
    client = Hrflow(api_secret=x_api_key, api_user=x_user_email)
    return client


def test_IndeedFeed(hrflow_client):
    action = IndeedFeed(
        executable_path=ChromeDriverManager().install(),
        max_page=2,
        subdomain="fr",
        hrflow_client=hrflow_client,
        job_search="Software Engineer",
        job_location="Paris",
        board_key="5865a71e45b94e29f7c1c97d71479ef2757df414",
        hydrate_with_parsing=True
    )
    action.execute()
