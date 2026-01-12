import os

import boto3
from botocore.exceptions import ClientError
from rich.console import Console
from rich.table import Table

console = Console()


def get_dynamodb_resource():
    return boto3.resource(
        "dynamodb",
        region_name="us-east-1",
        endpoint_url="http://localhost:8000",
        aws_access_key_id="dummy",
        aws_secret_access_key="dummy",
    )


def scan_table(table_name):
    dynamodb = get_dynamodb_resource()
    table = dynamodb.Table(table_name)

    try:
        response = table.scan()
        items = response.get("Items", [])

        console.print(f"\n[bold blue]Scanning table: {table_name}[/bold blue]")
        console.print(f"Found {len(items)} items")

        if items:
            t = Table(show_header=True, header_style="bold magenta")
            t.add_column("Partition Key", style="cyan")
            t.add_column("Data Sample")

            for item in items:
                # Try to guess the PK
                pk = (
                    item.get("user_id")
                    or item.get("quiz_id")
                    or item.get("id")
                    or "Unknown"
                )
                t.add_row(str(pk), str(item)[:100] + "...")

            console.print(t)
        else:
            console.print("[yellow]Table is empty[/yellow]")

    except ClientError as e:
        console.print(f"[red]Error scanning {table_name}: {e}[/red]")
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")


if __name__ == "__main__":
    console.print("[bold green]Checking Local DynamoDB Data[/bold green]")

    # Tables from environment or fallbacks
    users_table = os.getenv("DYNAMODB_USERS_TABLE", "users-development")
    archetype_profiles_table = os.getenv(
        "DYNAMODB_ARCHETYPE_PROFILES_TABLE", "user_archetype_profiles"
    )
    quiz_results_table = os.getenv(
        "DYNAMODB_QUIZ_RESULTS_TABLE", "archetype_quiz_results"
    )

    # Check the tables we expect data in
    scan_table(archetype_profiles_table)
    scan_table(quiz_results_table)
    scan_table(users_table)
