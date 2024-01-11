# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: : 2020-2023 The PyPSA-Eur Authors
#
# SPDX-License-Identifier: MIT
"""
Prepares brownfield data from previous planning horizon.
"""

import logging

logger = logging.getLogger(__name__)

import pandas as pd

idx = pd.IndexSlice

import numpy as np
import pypsa
import xarray as xr
from _helpers import update_config_with_sector_opts
from add_existing_baseyear import add_build_year_to_new_assets
from pypsa.clustering.spatial import normed_or_uniform


def add_brownfield(n, n_p, year):
    logger.info(f"Preparing brownfield for the year {year}")

    # electric transmission grid set optimised capacities of previous as minimum
    n.lines.s_nom_min = n_p.lines.s_nom_opt
    dc_i = n.links[n.links.carrier == "DC"].index
    n.links.loc[dc_i, "p_nom_min"] = n_p.links.loc[dc_i, "p_nom_opt"]

    for c in n_p.iterate_components(["Link", "Generator", "Store"]):
        attr = "e" if c.name == "Store" else "p"

        # first, remove generators, links and stores that track
        # CO2 or global EU values since these are already in n
        n_p.mremove(c.name, c.df.index[c.df.lifetime == np.inf])

        # remove assets whose build_year + lifetime < year
        n_p.mremove(c.name, c.df.index[c.df.build_year + c.df.lifetime < year])

        # remove assets if their optimized nominal capacity is lower than a threshold
        # since CHP heat Link is proportional to CHP electric Link, make sure threshold is compatible
        chp_heat = c.df.index[
            (c.df[f"{attr}_nom_extendable"] & c.df.index.str.contains("urban central"))
            & c.df.index.str.contains("CHP")
            & c.df.index.str.contains("heat")
        ]

        threshold = snakemake.params.threshold_capacity

        if not chp_heat.empty:
            threshold_chp_heat = (
                threshold
                * c.df.efficiency[chp_heat.str.replace("heat", "electric")].values
                * c.df.p_nom_ratio[chp_heat.str.replace("heat", "electric")].values
                / c.df.efficiency[chp_heat].values
            )
            n_p.mremove(
                c.name,
                chp_heat[c.df.loc[chp_heat, f"{attr}_nom_opt"] < threshold_chp_heat],
            )

        n_p.mremove(
            c.name,
            c.df.index[
                (c.df[f"{attr}_nom_extendable"] & ~c.df.index.isin(chp_heat))
                & (c.df[f"{attr}_nom_opt"] < threshold)
            ],
        )

        # copy over assets but fix their capacity
        c.df[f"{attr}_nom"] = c.df[f"{attr}_nom_opt"]
        c.df[f"{attr}_nom_extendable"] = False

        n.import_components_from_dataframe(c.df, c.name)

        # copy time-dependent
        selection = n.component_attrs[c.name].type.str.contains(
            "series"
        ) & n.component_attrs[c.name].status.str.contains("Input")
        for tattr in n.component_attrs[c.name].index[selection]:
            n.import_series_from_dataframe(c.pnl[tattr], c.name, tattr)

        # deal with gas network
        pipe_carrier = ["gas pipeline"]
        if snakemake.params.H2_retrofit:
            # drop capacities of previous year to avoid duplicating
            to_drop = n.links.carrier.isin(pipe_carrier) & (n.links.build_year != year)
            n.mremove("Link", n.links.loc[to_drop].index)

            # subtract the already retrofitted from today's gas grid capacity
            h2_retrofitted_fixed_i = n.links[
                (n.links.carrier == "H2 pipeline retrofitted")
                & (n.links.build_year != year)
            ].index
            gas_pipes_i = n.links[n.links.carrier.isin(pipe_carrier)].index
            CH4_per_H2 = 1 / snakemake.params.H2_retrofit_capacity_per_CH4
            fr = "H2 pipeline retrofitted"
            to = "gas pipeline"
            # today's pipe capacity
            pipe_capacity = n.links.loc[gas_pipes_i, "p_nom"]
            # already retrofitted capacity from gas -> H2
            already_retrofitted = (
                n.links.loc[h2_retrofitted_fixed_i, "p_nom"]
                .rename(lambda x: x.split("-2")[0].replace(fr, to))
                .groupby(level=0)
                .sum()
            )
            remaining_capacity = (
                pipe_capacity
                - CH4_per_H2
                * already_retrofitted.reindex(index=pipe_capacity.index).fillna(0)
            )
            n.links.loc[gas_pipes_i, "p_nom"] = remaining_capacity
        else:
            new_pipes = n.links.carrier.isin(pipe_carrier) & (
                n.links.build_year == year
            )
            n.links.loc[new_pipes, "p_nom"] = 0.0
            n.links.loc[new_pipes, "p_nom_min"] = 0.0


def disable_grid_expansion_if_LV_limit_hit(n):
    if not "lv_limit" in n.global_constraints.index:
        return

    total_expansion = (
        n.lines.eval("s_nom_min * length").sum()
        + n.links.query("carrier == 'DC'").eval("p_nom_min * length").sum()
    ).sum()

    lv_limit = n.global_constraints.at["lv_limit", "constant"]

    # allow small numerical differences
    if lv_limit - total_expansion < 1:
        logger.info(f"LV is already reached, disabling expansion and LV limit")
        extendable_acs = n.lines.query("s_nom_extendable").index
        n.lines.loc[extendable_acs, "s_nom_extendable"] = False
        n.lines.loc[extendable_acs, "s_nom"] = n.lines.loc[extendable_acs, "s_nom_min"]

        extendable_dcs = n.links.query("carrier == 'DC' and p_nom_extendable").index
        n.links.loc[extendable_dcs, "p_nom_extendable"] = False
        n.links.loc[extendable_dcs, "p_nom"] = n.links.loc[extendable_dcs, "p_nom_min"]

        n.global_constraints.drop("lv_limit", inplace=True)


def adjust_renewable_profiles(n, input_profiles, config, year):
    """
    Adjusts renewable profiles according to the renewable technology specified.

    If the planning horizon is not available, the closest year is used
    instead.
    """

    cluster_busmap = pd.read_csv(snakemake.input.cluster_busmap, index_col=0).squeeze()
    simplify_busmap = pd.read_csv(
        snakemake.input.simplify_busmap, index_col=0
    ).squeeze()
    clustermaps = simplify_busmap.map(cluster_busmap)
    clustermaps.index = clustermaps.index.astype(str)
    dr = pd.date_range(**config["snapshots"], freq="H")
    snapshotmaps = (
        pd.Series(dr, index=dr).where(lambda x: x.isin(n.snapshots), pd.NA).ffill()
    )

    for carrier in config["electricity"]["renewable_carriers"]:
        if carrier == "hydro":
            continue

    clustermaps.index = clustermaps.index.astype(str)
    dr = pd.date_range(**config["snapshots"], freq="H")
    snapshotmaps = (
        pd.Series(dr, index=dr).where(lambda x: x.isin(n.snapshots), pd.NA).ffill()
    )
    for carrier in config["electricity"]["renewable_carriers"]:
        if carrier == "hydro":
            continue
        with xr.open_dataset(getattr(input_profiles, "profile_" + carrier)) as ds:
            if ds.indexes["bus"].empty or "year" not in ds.indexes:
                continue
            if year in ds.indexes["year"]:
                p_max_pu = (
                    ds["year_profiles"]
                    .sel(year=year)
                    .transpose("time", "bus")
                    .to_pandas()
                )
            else:
                available_previous_years = [
                    available_year
                    for available_year in ds.indexes["year"]
                    if available_year < year
                ]
                available_following_years = [
                    available_year
                    for available_year in ds.indexes["year"]
                    if available_year > year
                ]
                if available_previous_years:
                    closest_year = max(available_previous_years)
                if available_following_years:
                    closest_year = min(available_following_years)
                logging.warning(
                    f"Planning horizon {year} not in {carrier} profiles. Using closest year {closest_year} instead."
                )
                p_max_pu = (
                    ds["year_profiles"]
                    .sel(year=closest_year)
                    .transpose("time", "bus")
                    .to_pandas()
                )
            # spatial clustering
            weight = ds["weight"].to_pandas()
            weight = weight.groupby(clustermaps).transform(normed_or_uniform)
            p_max_pu = (p_max_pu * weight).T.groupby(clustermaps).sum().T
            p_max_pu.columns = p_max_pu.columns + f" {carrier}"
            # temporal_clustering
            p_max_pu = p_max_pu.groupby(snapshotmaps).mean()
            # replace renewable time series
            n.generators_t.p_max_pu.loc[:, p_max_pu.columns] = p_max_pu


if __name__ == "__main__":
    if "snakemake" not in globals():
        from _helpers import mock_snakemake

        snakemake = mock_snakemake(
            "add_brownfield",
            simpl="",
            clusters="37",
            opts="",
            ll="v1.0",
            sector_opts="168H-T-H-B-I-solar+p3-dist1",
            planning_horizons=2030,
        )

    logging.basicConfig(level=snakemake.config["logging"]["level"])

    update_config_with_sector_opts(snakemake.config, snakemake.wildcards.sector_opts)

    logger.info(f"Preparing brownfield from the file {snakemake.input.network_p}")

    year = int(snakemake.wildcards.planning_horizons)

    n = pypsa.Network(snakemake.input.network)

    adjust_renewable_profiles(n, snakemake.input, snakemake.config, year)

    add_build_year_to_new_assets(n, year)

    n_p = pypsa.Network(snakemake.input.network_p)

    add_brownfield(n, n_p, year)

    disable_grid_expansion_if_LV_limit_hit(n)

    n.meta = dict(snakemake.config, **dict(wildcards=dict(snakemake.wildcards)))
    n.export_to_netcdf(snakemake.output[0])
