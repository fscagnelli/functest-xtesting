#!/usr/bin/env python

# Copyright (c) 2020 Orange and others.
#
# All rights reserved. This program and the accompanying materials
# are made available under the terms of the Apache License, Version 2.0
# which accompanies this distribution, and is available at
# http://www.apache.org/licenses/LICENSE-2.0

# pylint: disable=too-many-instance-attributes

"""Implement a Xtesting driver to run mts suite."""

import csv
import logging
import os
import shutil
import time

from lxml import etree
import prettytable

from xtesting.core import feature
from xtesting.core import testcase


__author__ = ("Vincent Mahe <v.mahe@orange.com>, "
              "Cedric Ollivier <cedric.ollivier@orange.com>")


class MTSLauncher(feature.BashFeature):
    """Class designed to run MTS tests."""

    __logger = logging.getLogger(__name__)
    mts_install_dir = "/opt/mts"

    def check_requirements(self):
        """Check if startCmd.sh is in /opt/mts/bin"""
        if not os.path.exists(
                os.path.join(self.mts_install_dir, 'bin/startCmd.sh')):
            self.__logger.warning(
                "mts is not available for arm for the time being")
            self.is_skipped = True

    def __init__(self, **kwargs):
        super(MTSLauncher, self).__init__(**kwargs)
        # Location of the HTML report generated by MTS
        self.mts_stats_dir = os.path.join(self.res_dir, 'mts_stats_report')
        # Location of the log files generated by MTS for each test.
        # Need to end path with a separator because of a bug in MTS.
        self.mts_logs_dir = os.path.join(self.res_dir,
                                         'mts_logs' + os.path.sep)
        # The location of file named testPlan.csv
        # that it always in $MTS_HOME/logs
        self.mts_result_csv_file = self.mts_install_dir + os.path.sep
        self.mts_result_csv_file += ("logs" + os.path.sep + "testPlan.csv")
        self.total_tests = 0
        self.pass_tests = 0
        self.fail_tests = 0
        self.skip_tests = 0
        self.response = None
        self.testcases = []

    def parse_results(self):
        """Parse testPlan.csv containing the status of each testcase of the test file.
        See sample file in `xtesting/samples/mts/output/testPlan.csv`
        """
        with open(self.mts_result_csv_file) as stream_:
            self.__logger.info("Parsing file : %s", self.mts_result_csv_file)
            reader = csv.reader(stream_, delimiter=';')
            rownum = 0
            _tests_data = []
            msg = prettytable.PrettyTable(
                header_style='upper', padding_width=5,
                field_names=['MTS test', 'MTS test case',
                             'status'])
            for row in reader:
                _test_dict = {}
                nb_values = len(row)
                if rownum > 0:
                    # If there's only one delimiter,
                    # it is the name of the <test> elt
                    if nb_values == 2:
                        test_name = row[0]
                        _test_dict['parent'] = test_name
                    elif nb_values == 3:
                        testcase_name = row[0].lstrip()
                        testcase_status = row[2]
                        self.total_tests += 1
                        if testcase_status == 'OK':
                            self.pass_tests += 1
                        elif testcase_status == 'Failed':
                            self.fail_tests += 1
                        elif testcase_status == '?':
                            self.skip_tests += 1
                        _test_dict['status'] = testcase_status
                        _test_dict['name'] = testcase_name
                        msg.add_row(
                            [test_name,
                             _test_dict['name'],
                             _test_dict['status']])
                rownum += 1
                _tests_data.append(_test_dict)
            try:
                self.result = 100 * (
                    self.pass_tests / self.total_tests)
            except ZeroDivisionError:
                self.__logger.error("No test has been run")
            self.__logger.info("MTS Test result:\n\n%s\n", msg.get_string())
            self.details = {}
            self.details['description'] = "Execution of some MTS tests"
            self.details['total_tests'] = self.total_tests
            self.details['pass_tests'] = self.pass_tests
            self.details['fail_tests'] = self.fail_tests
            self.details['skip_tests'] = self.skip_tests
            self.details['tests'] = _tests_data

    def parse_xml_test_file(self, xml_test_file):
        """Parse the XML file containing the test definition for MTS.
        See sample file in `xtesting/samples/mts/test.xml`
        """
        nb_testcases = -1
        self.__logger.info(
            "Parsing XML test file %s containing the MTS tests definitions.",
            xml_test_file)
        try:
            parser = etree.XMLParser(load_dtd=True, resolve_entities=True)
            self.__logger.info("XML test file %s successfully parsed.",
                               xml_test_file)
            root = etree.parse(xml_test_file, parser=parser)
            # Need to look at all child nodes because there may be
            # some <for> elt between <test> and <testcase> elt
            self.testcases = root.xpath('//test//testcase/@name')
            nb_testcases = len(self.testcases)
            if nb_testcases == 0:
                self.__logger.warning("Found no MTS testcase !")
            elif nb_testcases == 1:
                self.__logger.info("Found only one MTS testcase: %s",
                                   self.testcases[0])
            else:
                self.__logger.info("Found %d MTS testcases :", nb_testcases)
                for mts_testcase in self.testcases:
                    self.__logger.info("    - %s", mts_testcase)
        except etree.XMLSyntaxError as xml_err:
            self.__logger.error("Error while parsing XML test file: %s",
                                str(xml_err))
        return nb_testcases

    def check_enabled_mts_test_cases(self, enabled_testcases):
        """Make sure that all required MTS test cases exist
        in the XML test file.
        """
        if enabled_testcases:
            # Verify if the MTS test case exists in the whole list of test
            # cases declared in the test XML file
            for enabled_testcase in enabled_testcases:
                if enabled_testcase not in self.testcases:
                    self.__logger.error(
                        "The required MTS testcase named `%s` does not exist"
                        " !", enabled_testcase)
                    return False
        return True

    def execute(self, **kwargs):  # pylint: disable=too-many-locals
        try:
            # Read specific parameters for MTS
            test_file = kwargs["test_file"]
            log_level = kwargs[
                "log_level"] if "log_level" in kwargs else "INFO"
            store_method = kwargs[
                "store_method"] if "store_method" in kwargs else "FILE"
            # Must use the $HOME_MTS/bin as current working dir
            cwd = self.mts_install_dir + os.path.sep + "bin"

            # Get the list of enabled MTS testcases, if any
            enabled_testcases = kwargs[
                "testcases"] if "testcases" in kwargs else []
            enabled_testcases_str = ''
            if enabled_testcases:
                enabled_testcases_str = ' '.join(enabled_testcases)
                check_ok = self.check_enabled_mts_test_cases(enabled_testcases)
                if not check_ok:
                    return -3

            # Build command line to launch for MTS
            cmd = ("cd {} && ./startCmd.sh {} {} -sequential -levelLog:{}"
                   " -storageLog:{}"
                   " -config:stats.REPORT_DIRECTORY+{}"
                   " -config:logs.STORAGE_DIRECTORY+{}"
                   " -genReport:true"
                   " -showRep:false").format(cwd,
                                             test_file,
                                             enabled_testcases_str,
                                             log_level,
                                             store_method,
                                             self.mts_stats_dir,
                                             self.mts_logs_dir)

            # Make sure to create the necessary output sub-folders for MTS
            # and cleanup output files from previous run.
            if os.path.exists(self.mts_result_csv_file):
                os.remove(self.mts_result_csv_file)

            if os.path.isdir(self.mts_stats_dir):
                shutil.rmtree(self.mts_stats_dir)
            os.makedirs(self.mts_stats_dir)

            if os.path.isdir(self.mts_logs_dir):
                shutil.rmtree(self.mts_logs_dir)
            os.makedirs(self.mts_logs_dir)

            self.__logger.info(
                "MTS statistics output dir: %s ", self.mts_stats_dir)
            self.__logger.info("MTS logs output dir: %s ", self.mts_logs_dir)

            kwargs.pop("cmd", None)
            return super(MTSLauncher, self).execute(cmd=cmd, **kwargs)

        except KeyError:
            self.__logger.error("Missing mandatory arg for MTS. kwargs: %s",
                                kwargs)
        return -1

    def run(self, **kwargs):
        """Runs the MTS suite"""
        self.start_time = time.time()
        exit_code = testcase.TestCase.EX_RUN_ERROR
        self.result = 0
        try:
            nb_testcases = self.parse_xml_test_file(kwargs["test_file"])
            # Do something only if there are some MTS test cases in the test
            # file
            if nb_testcases > 0:
                if self.execute(**kwargs) == 0:
                    exit_code = testcase.TestCase.EX_OK
                    try:
                        self.parse_results()
                    except Exception:  # pylint: disable=broad-except
                        self.__logger.exception(
                            "Cannot parse result file "
                            "$MTS_HOME/logs/testPlan.csv")
                        exit_code = testcase.TestCase.EX_RUN_ERROR
        except Exception:  # pylint: disable=broad-except
            self.__logger.exception("%s FAILED", self.project_name)
        self.stop_time = time.time()
        return exit_code
