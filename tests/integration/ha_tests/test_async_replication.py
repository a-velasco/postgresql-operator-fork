#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
import contextlib
import logging
from asyncio import gather
from typing import Optional

import pytest as pytest
from juju.controller import Controller
from juju.model import Model
from lightkube import Client
from lightkube.resources.core_v1 import Pod
from pytest_operator.plugin import OpsTest

from tests.integration.ha_tests.helpers import (
    are_writes_increasing,
    check_writes,
    get_standby_leader,
    get_sync_standby,
    start_continuous_writes, app_name,
)
from tests.integration.helpers import (
    APPLICATION_NAME,
    DATABASE_APP_NAME,
    build_and_deploy,
    get_leader_unit,
    wait_for_relation_removed_between,
)
from tests.integration.juju_ import juju_major_version

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def fast_forward(
    model: Model, fast_interval: str = "10s", slow_interval: Optional[str] = None
):
    """Adaptation of OpsTest.fast_forward to work with different models."""
    update_interval_key = "update-status-hook-interval"
    if slow_interval:
        interval_after = slow_interval
    else:
        interval_after = (await model.get_config())[update_interval_key]

    await model.set_config({update_interval_key: fast_interval})
    yield
    await model.set_config({update_interval_key: interval_after})


@pytest.fixture(scope="module")
async def controller(first_model) -> Controller:
    """Return the controller."""
    return await first_model.get_controller()


@pytest.fixture(scope="module")
def first_model(ops_test: OpsTest) -> Model:
    """Return the first model."""
    first_model = ops_test.model
    return first_model


@pytest.fixture(scope="module")
async def second_model(controller, first_model) -> Model:
    """Create and return the second model."""
    second_model_name = f"{first_model.info.name}-other"
    await controller.add_model(second_model_name)
    second_model = Model()
    await second_model.connect(model_name=second_model_name)
    return second_model


@pytest.mark.group(1)
@pytest.mark.abort_on_fail
async def test_deploy_async_replication_setup(
    ops_test: OpsTest, first_model: Model, second_model: Model
) -> None:
    """Build and deploy two PostgreSQL cluster in two separate models to test async replication."""
    if not await app_name(ops_test):
        await build_and_deploy(ops_test, 3, wait_for_idle=False)
    if not await app_name(ops_test, second_model):
        await build_and_deploy(ops_test, 3, wait_for_idle=False, model=second_model)
    await ops_test.model.deploy(APPLICATION_NAME, num_units=1)

    async with ops_test.fast_forward(), fast_forward(second_model):
        await gather(
            first_model.wait_for_idle(
                apps=[DATABASE_APP_NAME, APPLICATION_NAME],
                status="active",
            ),
            second_model.wait_for_idle(
                apps=[DATABASE_APP_NAME],
                status="active",
            ),
        )


@pytest.mark.group(1)
@pytest.mark.abort_on_fail
async def test_async_replication(
    ops_test: OpsTest,
    controller: Controller,
    first_model: Model,
    second_model: Model,
    continuous_writes,
) -> None:
    """Test async replication between two PostgreSQL clusters."""
    logger.info("starting continuous writes to the database")
    await start_continuous_writes(ops_test, DATABASE_APP_NAME)

    logger.info("checking whether writes are increasing")
    await are_writes_increasing(ops_test)

    offer_endpoint = (
        f"{DATABASE_APP_NAME}:async-primary" if juju_major_version == 2 else "async-primary"
    )
    await first_model.create_offer(offer_endpoint, "async-primary", DATABASE_APP_NAME)
    await second_model.consume(
        f"admin/{first_model.info.name}.async-primary", controller=controller
    )

    async with ops_test.fast_forward("60s"), fast_forward(second_model, "60s"):
        await gather(
            first_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
            second_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
        )

    await second_model.relate(DATABASE_APP_NAME, "async-primary")

    async with ops_test.fast_forward("60s"), fast_forward(second_model, "60s"):
        await gather(
            first_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
            second_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
        )

    logger.info("checking whether writes are increasing")
    await are_writes_increasing(ops_test)

    # Run the promote action.
    logger.info("Get leader unit")
    leader_unit = await get_leader_unit(ops_test, DATABASE_APP_NAME)
    assert leader_unit is not None, "No leader unit found"
    logger.info("promoting the first cluster")
    run_action = await leader_unit.run_action("promote-standby-cluster")
    await run_action.wait()

    async with ops_test.fast_forward("60s"), fast_forward(second_model, "60s"):
        await gather(
            first_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
            second_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
        )

    logger.info("checking whether writes are increasing")
    await are_writes_increasing(ops_test)

    # Verify that no writes to the database were missed after stopping the writes
    # (check that all the units have all the writes).
    logger.info("checking whether no writes were lost")
    await check_writes(ops_test, extra_model=second_model)


@pytest.mark.group(1)
async def test_break_and_reestablish_relation(
    ops_test: OpsTest, first_model: Model, second_model: Model, continuous_writes
) -> None:
    """Test that the relation can be broken and re-established."""
    logger.info("starting continuous writes to the database")
    await start_continuous_writes(ops_test, DATABASE_APP_NAME)

    logger.info("checking whether writes are increasing")
    await are_writes_increasing(ops_test)

    logger.info("breaking the relation")
    await second_model.applications[DATABASE_APP_NAME].remove_relation(
        "async-replica", "async-primary"
    )
    wait_for_relation_removed_between(ops_test, "async-primary", "async-replica", second_model)
    async with ops_test.fast_forward("60s"), fast_forward(second_model, "60s"):
        await gather(
            first_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
            second_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
        )

    logger.info("reestablishing the relation")
    await second_model.relate(DATABASE_APP_NAME, "async-primary")
    async with ops_test.fast_forward("60s"), fast_forward(second_model, "60s"):
        await gather(
            first_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
            second_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
        )

    logger.info("checking whether writes are increasing")
    await are_writes_increasing(ops_test)

    # Run the promote action.
    logger.info("Get leader unit")
    leader_unit = await get_leader_unit(ops_test, DATABASE_APP_NAME)
    assert leader_unit is not None, "No leader unit found"
    logger.info("promoting the first cluster")
    run_action = await leader_unit.run_action("promote-standby-cluster")
    await run_action.wait()

    async with ops_test.fast_forward("60s"), fast_forward(second_model, "60s"):
        await gather(
            first_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
            second_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
        )

    logger.info("checking whether writes are increasing")
    await are_writes_increasing(ops_test)

    # Verify that no writes to the database were missed after stopping the writes
    # (check that all the units have all the writes).
    logger.info("checking whether no writes were lost")
    await check_writes(ops_test, extra_model=second_model)


@pytest.mark.group(1)
async def test_async_replication_failover_in_main_cluster(
    ops_test: OpsTest, first_model: Model, second_model: Model, continuous_writes
) -> None:
    """Test that async replication fails over correctly."""
    logger.info("starting continuous writes to the database")
    await start_continuous_writes(ops_test, DATABASE_APP_NAME)

    logger.info("checking whether writes are increasing")
    await are_writes_increasing(ops_test)

    sync_standby = await get_sync_standby(first_model, DATABASE_APP_NAME)
    logger.info(f"Sync-standby: {sync_standby}")
    logger.info("deleting the sync-standby pod")
    client = Client(namespace=first_model.info.name)
    client.delete(Pod, name=sync_standby.replace("/", "-"))

    async with ops_test.fast_forward("60s"), fast_forward(second_model, "60s"):
        await gather(
            first_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
            second_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
        )

    # Check that the sync-standby unit is not the same as before.
    new_sync_standby = await get_sync_standby(first_model, DATABASE_APP_NAME)
    logger.info(f"New sync-standby: {new_sync_standby}")
    assert new_sync_standby != sync_standby, "Sync-standby is the same as before"

    logger.info("Ensure continuous_writes after the crashed unit")
    await are_writes_increasing(ops_test)

    # Verify that no writes to the database were missed after stopping the writes
    # (check that all the units have all the writes).
    logger.info("checking whether no writes were lost")
    await check_writes(ops_test, extra_model=second_model)


@pytest.mark.group(1)
async def test_async_replication_failover_in_secondary_cluster(
    ops_test: OpsTest, first_model: Model, second_model: Model, continuous_writes
) -> None:
    """Test that async replication fails back correctly."""
    logger.info("starting continuous writes to the database")
    await start_continuous_writes(ops_test, DATABASE_APP_NAME)

    logger.info("checking whether writes are increasing")
    await are_writes_increasing(ops_test)

    standby_leader = await get_standby_leader(second_model, DATABASE_APP_NAME)
    logger.info(f"Standby leader: {standby_leader}")
    logger.info("deleting the standby leader pod")
    client = Client(namespace=second_model.info.name)
    client.delete(Pod, name=standby_leader.replace("/", "-"))

    async with ops_test.fast_forward("60s"), fast_forward(second_model, "60s"):
        await gather(
            first_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
            second_model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", idle_period=30),
        )

    # Check that the standby leader unit is not the same as before.
    new_standby_leader = await get_standby_leader(second_model, DATABASE_APP_NAME)
    assert new_standby_leader != standby_leader, "Standby leader is the same as before"

    logger.info("Ensure continuous_writes after the crashed unit")
    await are_writes_increasing(ops_test)

    # Verify that no writes to the database were missed after stopping the writes
    # (check that all the units have all the writes).
    logger.info("checking whether no writes were lost")
    await check_writes(ops_test, extra_model=second_model)
