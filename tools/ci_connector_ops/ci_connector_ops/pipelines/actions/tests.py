#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#

from typing import Tuple

from ci_connector_ops.pipelines.actions.environments import PYPROJECT_TOML_FILE_PATH
from ci_connector_ops.pipelines.utils import StepStatus, check_path_in_workdir, with_exit_code
from dagger import Client, Container, Directory

RUN_BLACK_CMD = ["python", "-m", "black", f"--config=/{PYPROJECT_TOML_FILE_PATH}", "--check", "."]
RUN_ISORT_CMD = ["python", "-m", "isort", f"--settings-file=/{PYPROJECT_TOML_FILE_PATH}", "--check-only", "--diff", "."]
RUN_FLAKE_CMD = ["python", "-m", "pflake8", f"--config=/{PYPROJECT_TOML_FILE_PATH}", "."]


async def _run_tests_in_directory(connector_container: Container, test_directory: str) -> StepStatus:
    """Runs the pytest tests in the test_directory that was passed.
    A StepStatus.SKIPPED is returned if no tests were discovered.
    Args:
        connector_container (Container): A connector containers with test dependencies installed.
        test_directory (str): The directory in which the tests are declared.

    Returns:
        StepStatus: Failure or success status of the tests.
    """
    test_config = "pytest.ini" if await check_path_in_workdir(connector_container, "pytest.ini") else "/" + PYPROJECT_TOML_FILE_PATH
    if await check_path_in_workdir(connector_container, test_directory):
        tester = connector_container.with_exec(
            [
                "python",
                "-m",
                "pytest",
                "-s",
                test_directory,
                "-c",
                test_config,
            ]
        )
        return StepStatus.from_exit_code(await with_exit_code(tester))
    else:
        return StepStatus.SKIPPED


async def check_format(connector_container: Container) -> StepStatus:
    """Run a code format check on the container source code.
    We call black, isort and flake commands:
    - Black formats the code: fails if the code is not formatted.
    - Isort checks the import orders: fails if the import are not properly ordered.
    - Flake enforces style-guides: fails if the style-guide is not followed.
    Args:
        connector_container (Container): _description_

    Returns:
        StepStatus: Failure or success status of the check.
    """
    formatter = (
        connector_container.with_exec(["echo", "Running black"])
        .with_exec(RUN_BLACK_CMD)
        .with_exec(["echo", "Running Isort"])
        .with_exec(RUN_ISORT_CMD)
        .with_exec(["echo", "Running Flake"])
        .with_exec(RUN_FLAKE_CMD)
    )
    return StepStatus.from_exit_code(await with_exit_code(formatter))


async def run_unit_tests(connector_container: Container) -> StepStatus:
    """Run all pytest tests declared in the unit_tests directory of the connector code.

    Args:
        connector_container (Container): A connector containers with test dependencies installed.

    Returns:
        StepStatus: Failure, skip or success status of the unit tests run.
    """
    return await _run_tests_in_directory(connector_container, "unit_tests")


async def run_integration_tests(connector_container: Container) -> StepStatus:
    """Run all pytest tests declared in the integration_tests directory of the connector code.

    Args:
        connector_container (Container): A connector containers with test dependencies installed.

    Returns:
        StepStatus: Failure, skip or success status of the integration tests run.
    """
    return await _run_tests_in_directory(connector_container, "integration_tests")


async def run_acceptance_tests(
    dagger_client: Client,
    connector_under_test_source_directory: Directory,
    connector_under_test_secret_directory: Directory,
    connector_under_test_image_id: str,
    connector_acceptance_test_image: str = "airbyte/connector-acceptance-test:latest",
) -> Tuple[StepStatus, Container, Directory]:
    """Runs the acceptance test suite on a connector under test. It's rebuilding the connector acceptance test image if the tag is :dev.

    Args:
        dagger_client (Client): The dagger client.
        connector_under_test_source_directory (Directory): The connector source code, required to access acceptance_test_config.yml and other versioned artifacts.
        connector_under_test_secret_directory (Directory): A directory in which the connector's secrets are stored, to be copied to /test_input/secrets.
        connector_under_test_image_id (str): Connector under test image id, used as a cachebuster.
        connector_acceptance_test_image (str, optional): The connector acceptance test image to use. Defaults to "airbyte/connector-acceptance-test:latest".

    Returns:
        Tuple[StepStatus, Directory]: The success/failure of the tests and a directory containing the updated secrets if any.
    """
    docker_host_socket = dagger_client.host().unix_socket("/var/run/docker.sock")

    if connector_acceptance_test_image.endswith(":dev"):
        cat_container = dagger_client.host().directory("airbyte-integrations/bases/connector-acceptance-test").docker_build()
    else:
        cat_container = dagger_client.container().from_(connector_acceptance_test_image)

    cat_container = (
        cat_container.with_unix_socket("/var/run/docker.sock", docker_host_socket)
        .with_workdir("/test_input")
        .with_env_variable("CACHEBUSTER", connector_under_test_image_id)
        .with_mounted_directory("/test_input", connector_under_test_source_directory)
        .with_directory("/test_input/secrets", connector_under_test_secret_directory)
        .with_entrypoint(["python", "-m", "pytest", "-p", "connector_acceptance_test.plugin", "-r", "fEsx"])
        .with_exec(["--acceptance-test-config", "/test_input"])
    )

    cat_container_step_status = StepStatus.from_exit_code(await with_exit_code(cat_container))
    secret_dir = cat_container.directory("/test_input/secrets")
    updated_secrets_dir = None
    if secret_files := await secret_dir.entries():
        for file_path in secret_files:
            if file_path.startswith("updated_configurations"):
                updated_secrets_dir = secret_dir
                break

    return cat_container_step_status, updated_secrets_dir


async def run_check(
    dagger_client: Client,
    secret_dir: Directory,
) -> Tuple[StepStatus, Container]:

    return await (
        dagger_client.container()
        .from_("airbyte/source-gitlab:latest")
        .with_directory("/secrets", secret_dir)
        .with_exec(["check", "--config", "/secrets/config_oauth.json"])
        .stdout()
    )
