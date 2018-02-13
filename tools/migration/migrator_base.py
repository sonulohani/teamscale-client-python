import json
import argparse
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from teamscale_client import TeamscaleClient
from teamscale_client.data import ServiceError
from requests.exceptions import ConnectionError


def get_arguments():
    """ Parses the arguments for the migration tool. """
    parser = argparse.ArgumentParser(description="test", formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("config", help="The path to the config file. Needs to be in a specific format, "
                                       "see config.template.")
    parser.add_argument("--debug", action="store_true", help="The debug option which enables debug log. Can be use to "
                                                             "dry-run the migration, as it does not change anything.")
    args = parser.parse_args()
    return load_config_json(args.config), args.debug


def load_config_json(path):
    """ Loads the config data as a JSON and returns it. """
    logger = MigratorBase.logger
    config_file = Path(path)
    if config_file.exists():
        try:
            return json.load(config_file.open())
        except json.JSONDecodeError:
            logger.exception("Config file '%s' is malformed" % path, exc_info=True)
    else:
        logger.exception("Config file '%s' does not exist" % path)
    exit(1)


def create_logger():
    """ Creates a logger """
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(levelname)-8s %(message)s (%(filename)-0s:%(lineno)-0s)")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


class MigratorBase(ABC):
    """ Base class for migrating data from one instance to another via REST calls. """
    logger = create_logger()

    def __init__(self, config_data, debug=False):
        self.debug = debug
        if self.debug:
            self.logger.setLevel(logging.DEBUG)
        else:
            self.logger.setLevel(logging.INFO)
        self.old, self.new = self.create_clients(config_data)
        self.versions_match = self.check_versions()
        self.migrated = 0
        self.cache = {}

        if self.debug:
            self.logger.debug("Debug Mode ON")

    def create_clients(self, config_data):
        """ Reads the given config defined by its path and creates the two teamscale clients from it.
        One old instance (migrating from) and a new onoe (migrating to).
        """
        try:
            return self.get_client(config_data["old_instance"]), self.get_client(config_data["new_instance"])
        except KeyError:
            self.logger.exception("Config data is malformed")
        except ConnectionError as e:
            self.logger.exception("Connection to %s could not be established" % e.request.url)
        except ServiceError:
            self.logger.exception("Creating the teamscale clients failed.")
        exit(1)

    def check_versions(self):
        """ Checks if the versions of both clients match. If not False will be returned
        and a warning will be logged.
        """
        old_version = self.old.get_version()
        new_version = self.new.get_version()
        if old_version != new_version:
            self.logger.warning("Teamscale versions of the old (%s) and new (%s) instance differ!" %
                                (old_version, new_version))
            return False
        return True

    @staticmethod
    def get_client(data):
        """ Creates a teamscale client from the given data """
        return TeamscaleClient(data["url"], data["user"], data["token"], data["project"])

    def check_cache(self, request, use_cache):
        """ If use_cache is True it checks if the cache already contains the response
        for the given request and returns it.
        If the cache shouldn't be used or no cache was found None is returned.
        """
        request = str(request)
        if use_cache and (request in self.cache):
            self.logger.debug("Cache hit for %s" % request)
            return self.cache[request]
        return None

    def cache_request(self, request, response, use_cache):
        """ If the cache should be used, the request and its response are cached. """
        if use_cache:
            self.cache[str(request)] = response

    def get(self, client, service, path_suffix="", parameters=None, use_cache=True):
        """ Performs a GET call from the client to the service with the given parameters
        and returns the response as a JSON Object.
        Args:
            path_suffix(str): Will be added to the end of the project service URL
        """
        url = client.get_project_service_url(service) + path_suffix

        response = self.check_cache((url, parameters), use_cache)
        if response is None:
            try:
                self.logger.debug("Service Call: {}".format((url, parameters)))
                response = client.get(url, parameters).json()
            except ServiceError as e:
                status_code = e.response.status_code
                if status_code in (500, 404):
                    self.logger.exception("Fetching data from %s failed (%s)" % (url, e.response.status_code))
                elif status_code == 400:
                    raise
        self.cache_request((url, parameters), response, use_cache)
        return response

    def get_from_old(self, service, path_suffix="", parameters=None, use_cache=True):
        """ Performs a GET call with the given information on the instance from
        which the data should be migrated and returns the response as a JSON Object.
        Args:
            path_suffix(str): Will be added to the end of the project service URL
        """
        return self.get(self.old, service, path_suffix, parameters, use_cache)

    def get_from_new(self, service, path_suffix="", parameters=None, use_cache=True):
        """ Performs a GET call with the given information on the instance
        to which the data should be migrated and returns the response as a JSON Object.
        Args:
            path_suffix(str): Will be added to the end of the project service URL
        """
        return self.get(self.new, service, path_suffix, parameters, use_cache)

    def put_in_new(self, service, data, path_suffix="", parameters=None):
        """ Performs a PUT call from the client to the service with the given parameters and data.
        The path parameter are additions to the path, e.g /service/id.
        Args:
            path_suffix(str): Will be added to the end of the project service URL
        """
        if not self.debug:
            self.new.put(self.new.get_project_service_url(service) + path_suffix,
                         parameters=parameters,
                         json=data)

    def get_matching_finding_id(self, finding_id):
        """ Tries to find a matching finding in the new instance for
        the given findings id of the old instance.
        If no match could be found `None` is returned.
        """
        finding = self.get_finding_by_id(self.old, finding_id)
        if finding is None:
            return None

        new_findings = self.get_from_new("findings", path_suffix=finding["location"]["uniformPath"])
        for new_finding in new_findings:
            if self.match_finding(new_finding, finding):
                return new_finding["id"]
        return None

    def get_finding_by_id(self, client, finding_id):
        """ Returns the finding with the specified id from the given client.
        If no finding with that ID can be found `None` is returned.
        """
        try:
            return self.get(client, "findings-by-id", path_suffix=finding_id)
        except ServiceError as e:
            if e.response.status_code == 400:
                self.logger.info("Finding with id %s not found. Skipping." % finding_id)
                return None

    def get_findings_url(self, findings_id, client=None):
        """ Creates a url link to the finding with the given id on the given Teamscale """
        if client is None:
            client = self.old
        return "{0.url}/findings.html#details/{0.project}/?id={1}".format(client, findings_id)

    def match_finding(self, finding1, finding2):
        """ Checks if the given two findings are the same. This is done by comparing their location and message.
        If the version of the two TS instances don't match, only the location is compared """
        location_match = finding1["location"] == finding2["location"]
        message_match = finding1["message"] == finding2["message"]
        return location_match and (message_match or not self.versions_match)

    @abstractmethod
    def migrate(self):
        """ Migrates the date from the old instance to the new one """
        pass
