import copy
import datetime
import time
import uuid
from typing import Optional

import pymongo
from tenacity import retry, retry_if_exception_type, stop_after_delay, wait_random

from fedn.common.log_config import logger
from fedn.network.combiner.interfaces import CombinerUnavailableError
from fedn.network.combiner.modelservice import load_model_from_bytes
from fedn.network.combiner.roundhandler import RoundConfig
from fedn.network.controller.controlbase import ControlBase
from fedn.network.state import ReducerState
from fedn.network.storage.s3.repository import Repository
from fedn.network.storage.statestore.stores.client_store import ClientStore
from fedn.network.storage.statestore.stores.combiner_store import CombinerStore
from fedn.network.storage.statestore.stores.dto.run import RunDTO
from fedn.network.storage.statestore.stores.dto.session import SessionConfigDTO
from fedn.network.storage.statestore.stores.model_store import ModelStore
from fedn.network.storage.statestore.stores.package_store import PackageStore
from fedn.network.storage.statestore.stores.round_store import RoundStore
from fedn.network.storage.statestore.stores.run_store import RunStore
from fedn.network.storage.statestore.stores.session_store import SessionStore


class UnsupportedStorageBackend(Exception):
    """Exception class for when storage backend is not supported. Passes"""

    def __init__(self, message):
        """Constructor method.

        :param message: The exception message.
        :type message: str

        """
        self.message = message
        super().__init__(self.message)


class MisconfiguredStorageBackend(Exception):
    """Exception class for when storage backend is misconfigured.

    :param message: The exception message.
    :type message: str
    """

    def __init__(self, message):
        """Constructor method."""
        self.message = message
        super().__init__(self.message)


class NoModelException(Exception):
    """Exception class for when model is None

    :param message: The exception message.
    :type message: str
    """

    def __init__(self, message):
        """Constructor method."""
        self.message = message
        super().__init__(self.message)


class CombinersNotDoneException(Exception):
    """Exception class for when model is None"""

    def __init__(self, message):
        """Constructor method.

        :param message: The exception message.
        :type message: str

        """
        self.message = message
        super().__init__(self.message)


class SessionTerminatedException(Exception):
    """Exception class for when session is terminated"""

    def __init__(self, message):
        """Constructor method.

        :param message: The exception message.
        :type message: str

        """
        self.message = message
        super().__init__(self.message)


class Control(ControlBase):
    """Controller, implementing the overall global training, validation and prediction logic.

    :param statestore: A StateStorage instance.
    :type statestore: class: `fedn.network.statestorebase.StateStorageBase`
    """

    def __init__(
        self,
        network_id: str,
        session_store: SessionStore,
        model_store: ModelStore,
        round_store: RoundStore,
        package_store: PackageStore,
        combiner_store: CombinerStore,
        client_store: ClientStore,
        model_repository: Repository,
        training_run_store: RunStore,
    ):
        """Constructor method."""
        super().__init__(network_id, session_store, model_store, round_store, package_store, combiner_store, client_store, model_repository, training_run_store)
        self.name = "DefaultControl"

    def _get_active_model_id(self, session_id: str) -> Optional[str]:
        """Get the active model for a session.

        :param session_id: The session ID.
        :type session_id: str
        :return: The active model ID.
        :rtype: str
        """
        last_model_of_session = self.model_store.list(1, 0, "committed_at", pymongo.DESCENDING, session_id=session_id)
        if len(last_model_of_session) > 0:
            return last_model_of_session[0].model_id

        # if no model is found for the session, get the last model in the model chain
        last_model = self.model_store.list(1, 0, "committed_at", pymongo.DESCENDING)
        if len(last_model) > 0:
            return last_model[0].model_id

        return None

    def start_session(self, session_id: str, rounds: int, round_timeout: int) -> None:
        if self._state == ReducerState.instructing:
            logger.info("Controller already in INSTRUCTING state. A session is in progress.")
            return

        try:
            active_model_id = self._get_active_model_id(session_id)
            if not active_model_id or active_model_id in ["", " "]:
                logger.warning("No model in model chain, please provide a seed model!")
                return
        except Exception:
            logger.error("Failed to get latest model of session and model chain.")
            return

        self._state = ReducerState.instructing

        session = self.session_store.get(session_id)

        if not session:
            logger.error("Session not found.")
            return

        session_config = session.session_config

        if not session_config:
            logger.error("Session not properly configured.")
            return

        if round_timeout is not None:
            session_config.round_timeout = round_timeout

        self._state = ReducerState.monitoring

        last_round = self.get_latest_round_id()

        aggregator = session_config.aggregator

        for combiner in self.network.get_combiners():
            combiner.set_aggregator(aggregator)
            if session_config.server_functions is not None:
                combiner.set_server_functions(session_config.server_functions)

        self.set_session_status(session_id, "Started")

        training_run_obj = RunDTO()
        training_run_obj.session_id = session_id
        training_run_obj.model_id = active_model_id
        training_run_obj.round_timeout = session_config.round_timeout
        training_run_obj.rounds = rounds

        training_run_obj = self.training_run_store.add(training_run_obj)

        for round in range(1, rounds + 1):
            if last_round:
                current_round = last_round + round
            else:
                current_round = round

            try:
                if self.get_session_status(session_id) == "Terminated":
                    logger.info("Session terminated.")
                    training_run_obj.completed_at = datetime.datetime.now()
                    training_run_obj.completed_at_model_id = self.model_store.get_active()
                    self.training_run_store.update(training_run_obj)
                    break
                _, round_data = self.round(session_config, str(current_round), session_id)
                logger.info("Round completed with status {}".format(round_data.status))
            except TypeError as e:
                logger.error("Failed to execute round: {0}".format(e))

            session_config.model_id = self._get_active_model_id(session_id)

        if self.get_session_status(session_id) == "Started":
            self.set_session_status(session_id, "Finished")
            training_run_obj.completed_at = datetime.datetime.now()
            training_run_obj.completed_at_model_id = self.model_store.get_active()
            self.training_run_store.update(training_run_obj)
            logger.info("Session finished.")
        self._state = ReducerState.idle

        self.set_session_config(session_id, session_config.to_dict())

    def prediction_session(self, config: RoundConfig) -> None:
        """Execute a new prediction session.

        :param config: The round config.
        :type config: PredictionConfig
        :return: None
        """
        if self._state == ReducerState.instructing:
            logger.info("Controller already in INSTRUCTING state. A session is in progress.")
            return

        if len(self.network.get_combiners()) < 1:
            logger.warning("Prediction round cannot start, no combiners connected!")
            return

        if "model_id" not in config.keys():
            config["model_id"] = self.model_store.get_active()

        config["committed_at"] = datetime.datetime.now()
        config["task"] = "prediction"
        config["rounds"] = str(1)
        config["clients_required"] = 1

        participating_combiners = self.get_participating_combiners(config)

        # Check if the policy to start the round is met, Default is number of combiners > 0
        round_start = self.evaluate_round_start_policy(participating_combiners)

        if round_start:
            logger.info("Prediction round start policy met, {} participating combiners.".format(len(participating_combiners)))
            for combiner, _ in participating_combiners:
                combiner.submit(config)
                logger.info("Prediction round submitted to combiner {}".format(combiner))

    def round(self, session_config: SessionConfigDTO, round_id: str, session_id: str):
        """Execute one global round.

        : param session_config: The session config.
        : type session_config: dict
        : param round_id: The round id.
        : type round_id: str

        """
        self.create_round({"round_id": round_id, "status": "Pending"})

        if len(self.network.get_combiners()) < 1:
            logger.warning("Round cannot start, no combiners connected!")
            self.set_round_status(round_id, "Failed")
            return None, self.round_store.get(round_id)

        # Assemble round config for this global round
        round_config = session_config.to_dict()
        round_config["rounds"] = 1
        round_config["round_id"] = round_id
        round_config["task"] = "training"
        round_config["session_id"] = session_id

        self.set_round_config(round_id, round_config)

        # Get combiners that are able to participate in the round, given round_config
        participating_combiners = self.get_participating_combiners(round_config)

        # Check if the policy to start the round is met
        round_start = self.evaluate_round_start_policy(participating_combiners)

        if round_start:
            logger.info("round start policy met, {} participating combiners.".format(len(participating_combiners)))
        else:
            logger.warning("Round start policy not met, skipping round!")
            self.set_round_status(round_id, "Failed")
            return None, self.round_store.get(round_id)

        # Ask participating combiners to coordinate model updates
        _ = self.request_model_updates(participating_combiners)
        # TODO: Check response

        # Wait until participating combiners have produced an updated global model,
        # or round times out.
        def do_if_round_times_out(result):
            logger.warning("Round timed out!")
            return True

        @retry(
            wait=wait_random(min=1.0, max=2.0),
            stop=stop_after_delay(session_config.round_timeout),
            retry_error_callback=do_if_round_times_out,
            retry=retry_if_exception_type(CombinersNotDoneException),
        )
        def combiners_done():
            round = self.round_store.get(round_id)
            session_status = self.get_session_status(session_id)
            if session_status == "Terminated":
                self.set_round_status(round_id, "Terminated")
                return False
            if len(round.combiners) < 1:
                logger.info("Waiting for combiners to update model...")
                raise CombinersNotDoneException("Combiners have not yet reported.")

            if len(round.combiners) < len(participating_combiners):
                logger.info("Waiting for combiners to update model...")
                raise CombinersNotDoneException("All combiners have not yet reported.")

            return True

        combiners_are_done = combiners_done()
        if not combiners_are_done:
            return None, self.round_store.get(round_id)

        # Due to the distributed nature of the computation, there might be a
        # delay before combiners have reported the round data to the db,
        # so we need some robustness here.
        @retry(wait=wait_random(min=0.1, max=1.0), retry=retry_if_exception_type(KeyError))
        def check_combiners_done_reporting():
            round = self.round_store.get(round_id)
            if len(round.combiners) != len(participating_combiners):
                raise KeyError("Combiners have not yet reported.")

        check_combiners_done_reporting()

        round = self.round_store.get(round_id)
        round_valid = self.evaluate_round_validity_policy(round.to_dict())
        if not round_valid:
            logger.error("Round failed. Invalid - evaluate_round_validity_policy: False")
            self.set_round_status(round_id, "Failed")
            return None, self.round_store.get(round_id)

        logger.info("Reducing combiner level models...")
        # Reduce combiner models into a new global model
        round_data = {}
        try:
            round = self.round_store.get(round_id)
            model, data = self.reduce(round.combiners.to_dict())
            round_data["reduce"] = data
            logger.info("Done reducing models from combiners!")
        except Exception as e:
            logger.error("Failed to reduce models from combiners, reason: {}".format(e))
            self.set_round_status(round_id, "Failed")
            return None, self.round_store.get(round_id)

        # Commit the new global model to the model trail
        if model is not None:
            logger.info("Committing global model to model trail...")
            tic = time.time()
            model_id = str(uuid.uuid4())
            self.commit(model_id, model, session_id)
            round_data["time_commit"] = time.time() - tic
            logger.info("Done committing global model to model trail.")
        else:
            logger.error("Failed to commit model to global model trail.")
            self.set_round_status(round_id, "Failed")
            return None, self.round_store.get(round_id)

        self.set_round_status(round_id, "Success")

        # 4. Trigger participating combiner nodes to execute a validation round for the current model
        if session_config.validate:
            combiner_config = session_config.to_dict()
            combiner_config["round_id"] = round_id
            combiner_config["model_id"] = self.model_store.get_active()
            combiner_config["task"] = "validation"
            combiner_config["session_id"] = session_id

            helper_type: str = None

            try:
                active_package = self.package_store.get_active()
                helper_type = active_package.helper
            except Exception:
                logger.error("Failed to get active helper")

            combiner_config["helper_type"] = helper_type

            validating_combiners = self.get_participating_combiners(combiner_config)

            for combiner, combiner_config in validating_combiners:
                try:
                    logger.info("Submitting validation round to combiner {}".format(combiner))
                    combiner.submit(combiner_config)
                except CombinerUnavailableError:
                    self._handle_unavailable_combiner(combiner)
                    pass

        self.set_round_data(round_id, round_data)
        self.set_round_status(round_id, "Finished")
        return model_id, self.round_store.get(round_id)

    def reduce(self, combiners):
        """Combine updated models from Combiner nodes into one global model.

        : param combiners: dict of combiner names(key) and model IDs(value) to reduce
        : type combiners: dict
        """
        meta = {}
        meta["time_fetch_model"] = 0.0
        meta["time_load_model"] = 0.0
        meta["time_aggregate_model"] = 0.0

        i = 1
        model = None

        for combiner in combiners:
            name = combiner["name"]
            model_id = combiner["model_id"]

            logger.info("Fetching model ({}) from model repository".format(model_id))

            try:
                tic = time.time()
                data = self.model_repository.get_model(model_id)
                meta["time_fetch_model"] += time.time() - tic
            except Exception as e:
                logger.error("Failed to fetch model from model repository {}: {}".format(name, e))
                data = None

            if data is not None:
                try:
                    tic = time.time()
                    helper = self.get_helper()
                    model_next = load_model_from_bytes(data, helper)
                    meta["time_load_model"] += time.time() - tic
                    tic = time.time()
                    model = helper.increment_average(model, model_next, 1.0, i)
                    meta["time_aggregate_model"] += time.time() - tic
                except Exception:
                    tic = time.time()
                    model = load_model_from_bytes(data, helper)
                    meta["time_aggregate_model"] += time.time() - tic
                i = i + 1

            self.model_repository.delete_model(model_id)

        return model, meta

    def predict_instruct(self, config):
        """Main entrypoint for executing the prediction compute plan.

        : param config: configuration for the prediction round
        """
        # Check/set instucting state
        if self.__state == ReducerState.instructing:
            logger.info("Already set in INSTRUCTING state")
            return
        self.__state = ReducerState.instructing

        # Check for a model chain
        if not self.statestore.latest_model():
            logger.warning("No model in model chain, please set seed model.")

        # Set reducer in monitoring state
        self.__state = ReducerState.monitoring

        # Start prediction round
        try:
            self.prediction_round(config)
        except TypeError:
            logger.error("Round failed.")

        # Set reducer in idle state
        self.__state = ReducerState.idle

    def prediction_round(self, config):
        """Execute a prediction round.

        : param config: configuration for the prediction round
        """
        # Init meta
        round_data = {}

        # Check for at least one combiner in statestore
        if len(self.network.get_combiners()) < 1:
            logger.warning("No combiners connected!")
            return round_data

        # Setup combiner configuration
        combiner_config = copy.deepcopy(config)
        combiner_config["model_id"] = self.model_store.get_active()
        combiner_config["task"] = "prediction"
        combiner_config["helper_type"] = self.statestore.get_framework()

        # Select combiners
        validating_combiners = self.get_participating_combiners(combiner_config)

        # Test round start policy
        round_start = self.check_round_start_policy(validating_combiners)
        if round_start:
            logger.info("Round start policy met, participating combiners {}".format(validating_combiners))
        else:
            logger.warning("Round start policy not met, skipping round!")
            return None

        # Synch combiners with latest model and trigger prediction
        for combiner, combiner_config in validating_combiners:
            try:
                combiner.submit(combiner_config)
            except CombinerUnavailableError:
                # It is OK if prediction fails for a combiner
                self._handle_unavailable_combiner(combiner)
                pass

        return round_data
