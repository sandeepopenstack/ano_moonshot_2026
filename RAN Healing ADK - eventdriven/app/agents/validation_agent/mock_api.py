from execution_mock_output import (
    generate_execution_output,
    publish_execution_output
)


def fetch_solution_execution_result():
    """
    Consume Stage 9 published execution output.
    """
    published = publish_execution_output(
        generate_execution_output()
    )
    return published["payload"]