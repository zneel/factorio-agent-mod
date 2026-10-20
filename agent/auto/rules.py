"""Base rules from factoriocheatsheet.com (data for Factorio 2.0.7, plain machines, no modules).
Every generator and sizing step reads its numbers from here, so the plan follows the cheat sheet.
"""
# Belts: items/s per belt (8 items per tile), max underground gap in tiles
BELT = {"transport-belt": 15, "fast-transport-belt": 30, "express-transport-belt": 45}
BELT_LANE = {k: v / 2 for k, v in BELT.items()}
UNDERGROUND_GAP = {"transport-belt": 4, "fast-transport-belt": 6, "express-transport-belt": 8}
PIPE_TO_GROUND_GAP = 10
# Inserters (items/s, base capacity, chest to chest; taking from a belt is slightly slower)
INSERTER = {"burner-inserter": 0.6, "inserter": 0.83, "long-handed-inserter": 1.2, "fast-inserter": 2.31,
            "bulk-inserter": 2.31}
FROM_BELT = 0.85      # factor when picking from a belt
# Machines: crafting speed
SPEED = {"assembling-machine-1": 0.5, "assembling-machine-2": 0.75, "assembling-machine-3": 1.25,
         "stone-furnace": 1, "steel-furnace": 2, "electric-furnace": 2, "chemical-plant": 1, "oil-refinery": 1,
         "rocket-silo": 1, "lab": 1}
# Mining: electric drill 0.5 ore/s (30 per yellow belt, 15 per lane), burner 0.25 ore/s
DRILL = {"electric-mining-drill": 0.5, "burner-mining-drill": 0.25}
DRILLS_PER_BELT = {"transport-belt": 30}
# Smelting: 3.2 s per plate (iron, copper, brick), 16 s per steel; stone furnaces burn 0.0225 coal/s
SMELT_TIME = {"iron-plate": 3.2, "copper-plate": 3.2, "stone-brick": 3.2, "steel-plate": 16}
FURNACES_PER_BELT_INPUT = {"stone-furnace": 48, "steel-furnace": 24, "electric-furnace": 24}
STEEL_PER_IRON_FURNACE = 1      # iron : steel furnaces 1:1
FURNACE_COAL = 0.0225
# Steam power: 1 offshore pump : 20 boilers : 40 steam engines = 36 MW; a boiler burns 1.8 MW (coal 4 MJ)
PUMP_WATER = 1200
BOILER_MW = 1.8
ENGINES_PER_BOILER = 2
BOILERS_PER_PUMP = 20
COAL_MJ = 4.0
BOILERS_PER_COAL_BELT = 34
# Oil (advanced processing + cracking to gas): 20 refineries : 5 heavy crackers : 17 light crackers;
# one refinery takes 20 crude/s and 10 water/s; solid fuel is best made from light oil
CRACKING = {"oil-refinery": 20, "heavy-oil-cracking": 5, "light-oil-cracking": 17}
REFINERY_CRUDE = 20
# Common build ratios (plain machines)
RATIOS = {("copper-cable", "electronic-circuit"): (3, 2), ("iron-stick", "rail"): (1, 2),
          ("iron-gear-wheel", "automation-science-pack"): (1, 10)}
# Science: research takes every pack at the same rate; build each colour to the same packs/min
# Rocket: 100 rocket parts, each 10 processing units + 10 low density structures + 10 rocket fuel
ROCKET_PARTS = 100
# Layout facts the generators rely on
FACTS = [
    "Inserters place on the far lane of a belt and take from the closest lane first.",
    "Mining drills output straight onto a belt or into a machine, no inserter needed.",
    "Splitters can filter items; underground belts can be side-loaded.",
    "Pipelines over 320x320 need a pump; pumps and tanks move at most 1200 fluid/s.",
    "Main bus: group plate belts so the factory can grow; balance only where needed.",
]


def boilers_for(mw):
    import math
    return math.ceil(mw / BOILER_MW)


def coal_drills_for(boilers, furnaces, other_coal_per_s=0.0):
    import math
    coal = boilers * BOILER_MW / COAL_MJ + furnaces * FURNACE_COAL + other_coal_per_s
    return math.ceil(coal / DRILL["electric-mining-drill"])
