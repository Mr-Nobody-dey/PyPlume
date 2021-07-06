import datetime
from pathlib import Path
import os
import subprocess
import sys

import numpy as np
import pandas as pd
from parcels import FieldSet, plotting
import scipy.spatial
import xarray as xr

from parcels_utils import HFRGrid
import plot_utils
import utils


def line_seg(x1, y1, x2, y2):
    return dict(
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        dom=(x1, x2) if x1 <= x2 else (x2, x1),
        rng=(y1, y2) if y1 <= y2 else (y2, y1),
        # check for vertical line
        slope=(y1 - y2) / (x1 - x2) if x1 - x2 != 0 else np.nan
    )


def valid_point(x, y, line):
    in_dom = line["dom"][0] <= x <= line["dom"][1]
    in_range = line["rng"][0] <= y <= line["rng"][1]
    return in_dom and in_range


def intersection_info(x, y, line):
    """
    Returns:
        intersection x, intersection y
    """
    # vertical line
    if np.isnan(line["slope"]):
        return line["x1"], y
    if line["slope"] == 0:
        return x, line["y1"]
    norm_slope = -1 / line["slope"]
    slope_d = norm_slope - line["slope"]
    int_d = (line["slope"] * -line["x1"] + line["y1"]) - (norm_slope * -x + y)
    x_int = int_d / slope_d
    y_int = norm_slope * (x_int - x) + y
    return x_int, y_int


class ParticlePlotFeature:
    """
    Represents additional points to plot and maybe track on top of the particles from a
    Parcels simulation.
    """
    def __init__(self, lats, lons, labels=None, segments=False, track_dist=0):
        self.lats = lats
        self.lons = lons
        self.points = np.array([lats, lons]).T
        self.kdtree = scipy.spatial.KDTree(self.points)
        self.labels = labels
        if self.labels is not None:
            if len(self.labels) != len(self.lats):
                raise ValueError("Labels must be the same length as lats/lons")
        if segments:
            self.segments = np.empty(len(self.lats) - 1, dtype=dict)
            for i in range(0, len(self.points) - 1):
                self.segments[i] = line_seg(self.lons[i], self.lats[i], self.lons[i + 1], self.lats[i + 1])
        else:
            self.segments = None
        self.track_dist = track_dist

    def count_near(self, p_lats, p_lons):
        counts = np.zeros(len(self.lats))
        for p_point in zip(p_lats, p_lons):
            for i, point in enumerate(self.points):
                if utils.haversine(p_point[0], point[0], p_point[1], point[1]) <= self.track_dist:
                    counts[i] += 1
        return counts

    def get_closest_dist(self, lat, lon):
        least_dist = np.inf
        closest_idx = self.kdtree.query([lat, lon])[1]
        # check distances to line segments
        if self.segments is not None:
            seg_check = []
            if closest_idx < len(self.points) - 1:
                seg_check.append(self.segments[closest_idx])
            if closest_idx > 0:
                seg_check.append(self.segments[closest_idx - 1])
            for seg in seg_check:
                lon_int, lat_int = intersection_info(lon, lat, seg)
                if valid_point(lon_int, lat_int, seg):
                    dist = utils.haversine(lat, lat_int, lon, lon_int)
                    least_dist = dist if dist < least_dist else least_dist
        # check distance to closest point
        pnt = self.points[closest_idx]
        dist = utils.haversine(lat, pnt[0], lon, pnt[1])
        least_dist = dist if dist < least_dist else least_dist
        return least_dist

    @classmethod
    def get_sd_stations(cls, path=None):
        # TODO there are constants here, move them
        if path is None:
            path = utils.MATLAB_DIR / "wq_stposition.mat"
        station_mat = scipy.io.loadmat(path)
        station_lats = station_mat["ywq"].flatten()
        station_lons = station_mat["xwq"].flatten()
        station_names = np.array([
            "Coronado (North Island)",
            "Silver Strand",
            "Silver Strand Beach",
            "Carnation Ave.",
            "Imperial Beach Pier",
            "Cortez Ave.",
            "End of Seacoast Dr.",
            "3/4 mi. N. of TJ River Mouth",
            "Tijuana River Mouth",
            "Monument Rd.",
            "Board Fence",
            "Mexico"
        ])
        return cls(station_lats, station_lons, labels=station_names, track_dist=500)


class TimedFrame:
    def __init__(self, time, path):
        self.time = time
        self.path = path

    def __repr__(self):
        return f"([{self.path}] at [{self.time}])"


class ParticleResult:
    """
    Wraps the output of a particle file to make visualizing and analyzing the results easier.
    Can also use an HFRGrid if the ocean currents are also needed.
    """
    PLOT_SAVE_DEFAULT = utils.FILES_ROOT / utils.PICUTRE_DIR

    def __init__(self, dataset):
        if isinstance(dataset, (Path, str)):
            self.path = dataset
            with xr.open_dataset(dataset) as ds:
                self.xrds = ds
        elif isinstance(dataset, xr.Dataset):
            self.path = None
            self.xrds = dataset
        else:
            raise TypeError(f"{dataset} is not a path or xarray dataset")
        self.lats = self.xrds["lat"].values
        self.lons = self.xrds["lon"].values
        self.traj = self.xrds["trajectory"].values
        self.times = self.xrds["time"].values
        # not part of Ak4 kernel
        self.lifetimes = self.xrds["lifetime"].values
        self.spawntimes = self.xrds["spawntime"].values
        
        self.grid = None
        self.plot_features = []

    def add_grid(self, grid: HFRGrid):
        self.grid = grid
        gtimes, glats, glons = grid.get_coords()
        # check if particle set is in-bounds of the given grid
        if np.nanmin(self.times) < gtimes.min() or np.nanmax(self.times) > gtimes.max():
            raise ValueError("Time out of bounds")
        if np.nanmin(self.lats) < glats.min() or np.nanmax(self.lats) > glats.max():
            raise ValueError("Latitude out of bounds")
        if np.nanmin(self.lons) < glons.min() or np.nanmax(self.lons) > glons.max():
            raise ValueError("Longitude out of bounds")

    def add_plot_feature(self, feature: ParticlePlotFeature, station=False):
        self.plot_features.append((feature, station))

    def plot_feature(self, t, feature, station, ax):
        if station:
            curr_lats = self.lats[:, t]
            curr_lons = self.lons[:, t]
            counts = feature.count_near(curr_lats, curr_lons)
            ax.scatter(
                feature.lons[counts == 0], feature.lats[counts == 0], c="b", s=60, edgecolor="k"
            )
            ax.scatter(
                feature.lons[counts > 0], feature.lats[counts > 0], c="r", s=60, edgecolor="k"
            )
        else:
            ax.scatter(feature.lons, feature.lats)
            if feature.segments is not None:
                ax.plot(feature.lons, feature.lats)
        return ax

    def get_time(self, t):
        curr_time = self.times[:, t]
        non_nat = curr_time[~np.isnan(curr_time)]
        if len(non_nat) == 0:
            return np.datetime64("Nat")
        return non_nat[0]

    def plot_at_t(self, t, domain=None):
        if self.grid is None and domain is None:
            domain = {
                "W": np.nanmin(self.lons),
                "E": np.nanmax(self.lons),
                "S": np.nanmin(self.lats),
                "N": np.nanmax(self.lats),
            }
            domain = plot_utils.pad_domain(domain, 0.0005)
        elif self.grid is not None and domain is None:
            domain = self.grid.get_domain()
        max_life = np.nanmax(self.lifetimes)
        timestamp = self.get_time(t)
        if self.grid is None:
            fig, ax = plot_utils.get_carree_axis(domain, land=True)
            plot_utils.get_carree_gl(ax)
        else:
            show_time = int((timestamp - self.grid.times[0]) / np.timedelta64(1, "s"))
            if show_time < 0:
                raise ValueError("Particle simulation time domain goes out of bounds")
            _, fig, ax, _ = plotting.plotfield(field=self.grid.fieldset.UV, show_time=show_time,
                                            domain=domain, land=True, vmin=0, vmax=0.6,
                                            titlestr="Particles and ")
        non_nan = ~np.isnan(self.lons[:, t])
        ax.scatter(
            self.lons[:, t][non_nan], self.lats[:, t][non_nan], c=self.lifetimes[:, t][non_nan],
            edgecolor="k", vmin=0, vmax=max_life, s=25
        )
        for feature, station in self.plot_features:
            self.plot_feature(t, feature, station, ax)
        return fig, ax

    def generate_all_plots(self, save_dir, figsize=None, domain=None):
        frames = []
        for t in range(self.lats.shape[1]):
            savefile = os.path.join(save_dir, f"snap{t}.png")
            fig, ax = self.plot_at_t(t, domain=domain)
            plot_utils.draw_plt(savefile=savefile, fig=fig, figsize=figsize)
            frames.append(TimedFrame(self.get_time(t), savefile))
        return frames

    def generate_gif(self, frames, gif_path, gif_delay=25):
        input_paths = [str(frame.path) for frame in frames]
        sp_in = ["magick", "-delay", str(gif_delay)] + input_paths
        sp_in.append(str(gif_path))
        magick_sp = subprocess.Popen(
            sp_in,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        stdout, stderr = magick_sp.communicate()
        print(f"magick ouptput: {(stdout, stderr)}", file=sys.stderr)
        return gif_path
