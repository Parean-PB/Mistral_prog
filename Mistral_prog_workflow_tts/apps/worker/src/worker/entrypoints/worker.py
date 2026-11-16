"""Point d'entree du worker : decouvre les workflows du package et les sert."""

import asyncio

import mistralai.workflows as workflows

import worker.workflows  # noqa: F401  ( enregistre les workflows )


async def main() -> None:
    workflow_classes = workflows.discover_all_workflows_in_package("worker.workflows")
    if not workflow_classes:
        raise RuntimeError("Aucun workflow decouvert dans worker.workflows")
    await workflows.run_worker(workflow_classes)


if __name__ == "__main__":
    asyncio.run(main())
