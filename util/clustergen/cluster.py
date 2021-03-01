#!/usr/bin/env python3
# Copyright 2020 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from enum import Enum
from jsonschema import ValidationError, RefResolver, Draft7Validator, validators
from mako.lookup import TemplateLookup
from math import ceil, log2

import json
import re
import logging as log
import pathlib


# Fill in default values for config values which do not have a user-defined value.
def extend_with_default(validator_class):
    validate_properties = validator_class.VALIDATORS["properties"]

    def set_defaults(validator, properties, instance, schema):
        for property, subschema in properties.items():
            if "default" in subschema:
                instance.setdefault(property, subschema["default"])

        for error in validate_properties(
                validator,
                properties,
                instance,
                schema,
        ):
            yield error

    return validators.extend(
        validator_class,
        {"properties": set_defaults},
    )


DefaultValidatingDraft7Validator = extend_with_default(Draft7Validator)


class Generator(object):
    DISCLAIMER = """// AUTOMATICALLY GENERATED by clustergen.py; edit the script or configuration
// instead."""

    file_path = pathlib.Path(__file__).parent
    snitch_cluster_folder = file_path / "../../hw/ip/snitch_cluster"

    templates = TemplateLookup(directories=[snitch_cluster_folder],
                               output_encoding="utf-8")
    """
    Generator class which contains common component to generate different systems.
    @root_schema: Schema object to which the generator corresponds.
    """
    def __init__(self, root_schema):
        # Load the cluster schema.
        absolute_path_to_schema_dir = self.file_path / "../../docs/schema"
        root_schema_filename = root_schema

        self.root_schema = read_schema(absolute_path_to_schema_dir /
                                       root_schema_filename)

        store_set = dict()

        # iterate over schema directory and generate a mapping from remote URLs
        # to local URIs.
        for path in absolute_path_to_schema_dir.iterdir():
            schema = read_schema(path)
            store_set[schema["$id"]] = schema

        # Instantiate a custom resolver with the update store set.
        self.resolver = RefResolver.from_schema(self.root_schema,
                                                store=store_set)

    def validate(self, cfg):
        # Validate the schema. This can fail.
        try:
            DefaultValidatingDraft7Validator(
                self.root_schema, resolver=self.resolver).validate(cfg)
        except ValidationError as e:
            print(e)
            exit(e)


@dataclass
class RiscvISA:
    """Contain a valid base ISA string"""
    i: bool = False
    e: bool = False
    m: bool = False
    a: bool = False
    f: bool = False
    d: bool = False

    isa_string = re.compile(r"^rv32(i|e)([m|a|f|d]*)$")


def parse_isa_string(s):
    """Construct an `RiscvISA` object from a string"""
    s.lower()
    isa = RiscvISA()
    m = RiscvISA.isa_string.match(s)
    if m:
        setattr(isa, m.group(1), True)
        if m.group(2):
            [setattr(isa, t, True) for t in m.group(2)]
    else:
        raise ValueError("Illegal ISA string.")

    return isa


class PMA(Enum):
    # Region supports atomics
    ATOMIC = 1
    # Region is cached
    CACHED = 2
    # Region is execute
    EXECUTE = 3
    # Region is non-idempotent
    NON_IDEMPOTENT = 4


class PMACfg(object):
    def __init__(self):
        self.regions = list()

    def add_region(self, pma, base, mask):
        self.regions.append((pma, base, mask))


class SnitchCluster(Generator):
    """
    Instance of a Snitch cluster.
    """
    files = {
        'cfg': "src/snitch_cfg.sv.tpl",
        'wrapper': "src/snitch_cluster_wrapper.sv.tpl"
    }

    def __init__(self, cfg, pma_cfg):
        """
        Initialize with a given configuration. The constructor checks conformans
        to the cluster schema and constructs a `cfg` object.
        """
        super().__init__("snitch_cluster.schema.json")
        self.mems = set()
        self.mems_desc = dict()
        self.validate(cfg)
        self.cfg = cfg
        # Perform configuration validation.
        if self.cfg_validate():
            exit("Failed parameter validation.")

        self.cfg['pkg_name'] = "{}_pkg".format(self.cfg['name'])
        self.calc_cache_sizes()

        self.parse_pma_cfg(pma_cfg)
        self.parse_cores()

    def l1_region(self):
        """Return L1 Region as tuple. Base and length."""
        return (self.cfg['cluster_base_addr'], self.cfg['tcdm']['size'])

    def render_wrapper(self):
        """Render the cluster wrapper"""
        cfg_template = self.templates.get_template(self.files['wrapper'])
        return cfg_template.render_unicode(cfg=self.cfg,
                                           to_sv_hex=to_sv_hex,
                                           disclaimer=self.DISCLAIMER)

    def add_mem(self,
                words,
                width,
                byte_enable=True,
                desc=None,
                speed_optimized=True,
                density_optimized=False):
        mem = (
            width,  # width
            words,  # words
            8,  # byte_width
            1,  # ports
            1,  # latency
            byte_enable,  # byte_enable
            speed_optimized,  # speed optimized
            density_optimized,  # density optimized
        )
        self.mems.add(mem)
        if mem in self.mems_desc:
            self.mems_desc[mem] += [desc]
        else:
            self.mems_desc[mem] = [desc or ""]

    def memory_cfg(self):
        # Add TCDMs
        self.add_mem(self.cfg['tcdm']['depth'],
                     self.cfg['data_width'],
                     desc='tcdm')
        # Add instruction caches
        for i, h in enumerate(self.cfg['hives']):
            self.add_mem(h['icache']['depth'],
                         h['icache']['cacheline'],
                         desc='icache data (hive {})'.format(i),
                         byte_enable=False)

            self.add_mem(h['icache']['depth'],
                         self.tag_width,
                         desc='icache tag (hive {})'.format(i),
                         byte_enable=False)

        cfg = list()
        for mem in self.mems:
            cfg.append({
                'description': self.mems_desc[mem],
                'width': mem[0],
                'words': mem[1],
                'byte_width': mem[2],
                'ports': mem[3],
                'latency': mem[4],
                'byte_enable': mem[5],
                'speed_optimized': mem[6],
                'density_optimized': mem[7],
            })
        return json.dumps(cfg, sort_keys=True, indent=4)

    def calc_cache_sizes(self):
        # Calculate TCDM parameters
        tcdm_bytes = self.cfg['data_width'] // 8
        self.cfg['tcdm']['depth'] = self.cfg['tcdm']['size'] * 1024 // (
            self.cfg['tcdm']['banks'] * tcdm_bytes)
        # Calc icache parameters
        for i, hive in enumerate(self.cfg['hives']):
            cl_bytes = self.cfg['hives'][i]['icache']['cacheline'] // 8
            self.cfg['hives'][i]['icache']['depth'] = self.cfg['hives'][i][
                'icache']['size'] * 1024 // self.cfg['hives'][i]['icache'][
                    'sets'] // cl_bytes
            # tag width
            self.tag_width = self.cfg['addr_width'] - clog2(
                    hive['icache']['cacheline'] // 8) - clog2(hive['icache']['depth']) + 3

    def parse_pma_cfg(self, pma_cfg):
        self.cfg['pmas'] = dict()
        # print(pma_cfg.regions)
        self.cfg['pmas']['cached'] = list()
        for pma in pma_cfg.regions:
            if pma[0] == PMA.CACHED:
                self.cfg['pmas']['cached'].append((pma[1], pma[2]))

    def parse_cores(self):
        """Parse cores struct"""
        def gen_mask(c, s):
            return "{}'b{}".format(c, ''.join(reversed(s)))

        cores = list()
        for i, core_list in enumerate(self.cfg['hives']):
            for core in core_list['cores']:
                core['hive'] = i
                core['isa_parsed'] = parse_isa_string(
                    core['isa'])

                cores.append(dict(core))

        self.cfg['nr_hives'] = len(self.cfg['hives'])
        self.cfg['nr_cores'] = len(cores)
        self.cfg['cores'] = cores

    def cfg_validate(self):
        failed = True
        """Perform more advanced validation, i.e., sanity check parameters."""
        if int(self.cfg['addr_width']) < 30:
            log.error("`addr_width` must be greater or equal to 30.")
        elif not ((int(self.cfg['data_width']) == 32) or
                  (int(self.cfg['data_width']) == 64)):
            log.error("`data_width` must be 32 or 64 bit")
        elif int(self.cfg['dma_data_width']) <= 0:
            log.error("`dma_data_width` must be set")
        elif int(self.cfg['dma_data_width']) % int(
                self.cfg['data_width']) != 0:
            log.error(
                "DMA port {} has to be multiple of {} (bank width)".format(
                    self.cfg['dma_data_width'], self.cfg['data_width']))
        elif is_pow2(self.cfg['dma_data_width']):
            log.error("`dma_data_width` must be a power of two")
        # elif cfg.en_rvd and not cfg.en_rvf:
        #     log.error("RVD needs RVF")
        # elif cfg.en_rvd and not cfg.data_width == 64:
        #     log.error("RVD needs 64 bit data buses")
        elif (self.cfg['tcdm']['size'] % self.cfg['tcdm']['banks']) != 0:
            log.error(
                "The total size of the TCDM must be divisible by the requested amount of banks."
            )
        elif is_pow2(self.cfg['tcdm']['size']):
            log.error("The TCDM size must be a power of two.")
        elif is_pow2(self.cfg['tcdm']['banks']):
            log.error("The amount of banks must be a power of two.")
        else:
            failed = False

        # Warnings
        if (int(self.cfg['dma_data_width']) != 512):
            log.warn("Design was never tested with this configuration")

        return failed


class SnitchClusterTB(Generator):
    """
    A very simplistic system, which instantiates a single cluster and
    surrounding DRAM to test and simulate this system. This can also serve as a
    starting point on how to use the `snitchgen` library to generate more
    complex systems.
    """
    def __init__(self, cfg):
        super().__init__("snitch_cluster_tb.schema.json")
        # Validate the schema.
        self.validate(cfg)
        # from here we know that we have a valid object.
        # and construct a new SnitchClusterTB object.
        self.cfg = cfg
        pma_cfg = PMACfg()
        # TODO(zarubaf): Check dram start address is aligned to its length.
        # For this example system make the entire dram cacheable.
        pma_cfg.add_region(PMA.CACHED, self.cfg['dram']['address'],
                           self.cfg['dram']['length'])
        self.cfg['cluster']['tie_ports'] = True
        # Store Snitch cluster config in separate variable
        self.cluster = SnitchCluster(cfg["cluster"], pma_cfg)

    def render_wrapper(self):
        return self.cluster.render_wrapper()

    def render_linker_script(self):
        """Generate a linker script for the cluster testbench"""
        cfg_template = self.templates.get_template("test/link.ld.tpl")
        return cfg_template.render_unicode(cfg=self.cfg,
                                           l1_region=self.cluster.l1_region())

    def render_bootdata(self):
        """Generate a C file with boot information for the cluster testbench"""
        cfg_template = self.templates.get_template("test/bootdata.cc.tpl")
        return cfg_template.render_unicode(cfg=self.cfg)

    def render_deps(self, dep_name):
        return self.cluster.render_deps(dep_name)


def read_schema(path):
    """Read a single schema file and return the parsed JSON content.
    Aborts if the JSON file could not be decoed."""
    with open(path, "r") as f:
        try:
            schema = json.load(f)
        except json.decoder.JSONDecodeError as e:
            exit("Invalid schema file: {}".format(e))
    return schema


def clog2(x):
    """Returns the ceiled integer logarithm dualis."""
    return int(ceil(log2(x)))


def is_pow2(x):
    return 2**clog2(x) != x


def to_sv_hex(x, length=None):
    return "{}'h{}".format(length or "", hex(x)[2:])
