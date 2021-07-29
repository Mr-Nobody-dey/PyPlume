"""Create custom particle classes and kernels inside here"""
import math
from operator import attrgetter

import numpy as np
from parcels import JITParticle, Variable
from parcels import ParcelsRandom


ParcelsRandom.seed(42)


class ThreddsParticle(JITParticle):
    """
    Not actually Thredds specific, just a particle that tracks its own lifetime, spawntime,
    and when it goes out of bounds.
    """
    lifetime = Variable("lifetime", initial=0, dtype=np.float32)
    spawntime = Variable("spawntime", initial=attrgetter("time"), dtype=np.float32)
    # out of bounds
    oob = Variable("oob", initial=0, dtype=np.int32)


def AgeParticle(particle, fieldset, time):
    """Kernel to age particles."""
    particle.lifetime += particle.dt


def RandomWalk(particle, fieldset, time):
    """
    Adds randomness to particle movement (doesn't actually work, ripped from the plume tracker).
    """
    cv = 1e-5 * 3600
    uerr = 500
    th = 2 * math.pi * ParcelsRandom.random()
    u_, v_ = fieldset.UV[time, particle.depth, particle.lat, particle.lon]
    # convert from degrees/s to m/s
    u_conv = 1852 * 60 * math.cos(particle.lat * math.pi / 180)
    v_conv = 1852 * 60
    u_ *= u_conv
    v_ *= v_conv
    u_n = u_ + uerr * math.cos(th)
    v_n = v_ + uerr * math.sin(th)
    dx = u_n * cv
    dy = v_n * cv
    dx /= u_conv
    dy /= v_conv
    particle.lon += dx
    particle.lat += dy


def TestOOB(particle, fieldset, time):
    """
    Kernel to test if a particle has gone into a location without any ocean current data.
    """
    OOB_THRESH = 1e-14
    u, v = fieldset.UV[time, particle.depth, particle.lat, particle.lon]
    if math.fabs(u) < OOB_THRESH and math.fabs(v) < OOB_THRESH:
        particle.oob = 1
    else:
        particle.oob = 0


def DeleteOOB(particle, fieldset, time):
    """Deletes particles that go out of bounds"""
    OOB_THRESH = 1e-14
    u, v = fieldset.UV[time, particle.depth, particle.lat, particle.lon]
    if math.fabs(u) < OOB_THRESH and math.fabs(v) < OOB_THRESH:
        particle.delete()


def DeleteAfter3Days(particle, fieldset, time):
    """Deletes a particle after 3 days (should probably rename this for that)"""
    LIFETIME = 259200
    if particle.lifetime > LIFETIME:
        particle.delete()


def DeleteParticle(particle, fieldset, time):
    """Deletes a particle. Mainly for use with the recovery kernel."""
    # print(f"Particle [{particle.id}] lost "
    #       f"({particle.time}, {particle.depth}, {particle.lat}, {particle.lon})", file=sys.stderr)
    particle.delete()
