"""
Module for dealing with 'gvar'-style font variations, also known as run-time
interpolation.

The ideas here are very similar to MutatorMath.  There is even code to read
MutatorMath .designspace files in the varLib.designspace module.

For now, if you run this file on a designspace file, it tries to find
ttf-interpolatable files for the masters and build a variable-font from
them.  Such ttf-interpolatable and designspace files can be generated from
a Glyphs source, eg., using noto-source as an example:

	$ fontmake -o ttf-interpolatable -g NotoSansArabic-MM.glyphs

Then you can make a variable-font this way:

	$ fonttools varLib master_ufo/NotoSansArabic.designspace

API *will* change in near future.
"""
from __future__ import print_function, division, absolute_import
from __future__ import unicode_literals
from fontTools.misc.py23 import *
from fontTools.misc.fixedTools import otRound
from fontTools.misc.arrayTools import Vector
from fontTools.ttLib import TTFont, newTable, TTLibError
from fontTools.ttLib.tables._n_a_m_e import NameRecord
from fontTools.ttLib.tables._f_v_a_r import Axis, NamedInstance
from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates
from fontTools.ttLib.tables.ttProgram import Program
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables.otBase import OTTableWriter
from fontTools.varLib import builder, models, varStore
from fontTools.varLib.merger import VariationMerger
from fontTools.varLib.mvar import MVAR_ENTRIES
from fontTools.varLib.iup import iup_delta_optimize
from fontTools.varLib.featureVars import addFeatureVariations
from fontTools.designspaceLib import DesignSpaceDocument, AxisDescriptor
from collections import OrderedDict, namedtuple
import os.path
import logging
from copy import deepcopy
from pprint import pformat

log = logging.getLogger("fontTools.varLib")


class VarLibError(Exception):
	pass

#
# Creation routines
#

def _add_fvar(font, axes, instances):
	"""
	Add 'fvar' table to font.

	axes is an ordered dictionary of DesignspaceAxis objects.

	instances is list of dictionary objects with 'location', 'stylename',
	and possibly 'postscriptfontname' entries.
	"""

	assert axes
	assert isinstance(axes, OrderedDict)

	log.info("Generating fvar")

	fvar = newTable('fvar')
	nameTable = font['name']

	for a in axes.values():
		axis = Axis()
		axis.axisTag = Tag(a.tag)
		# TODO Skip axes that have no variation.
		axis.minValue, axis.defaultValue, axis.maxValue = a.minimum, a.default, a.maximum
		axis.axisNameID = nameTable.addMultilingualName(a.labelNames, font)
		axis.flags = int(a.hidden)
		fvar.axes.append(axis)

	for instance in instances:
		coordinates = instance.location

		if "en" not in instance.localisedStyleName:
			assert instance.styleName
			localisedStyleName = dict(instance.localisedStyleName)
			localisedStyleName["en"] = tounicode(instance.styleName)
		else:
			localisedStyleName = instance.localisedStyleName

		psname = instance.postScriptFontName

		inst = NamedInstance()
		inst.subfamilyNameID = nameTable.addMultilingualName(localisedStyleName)
		if psname is not None:
			psname = tounicode(psname)
			inst.postscriptNameID = nameTable.addName(psname)
		inst.coordinates = {axes[k].tag:axes[k].map_backward(v) for k,v in coordinates.items()}
		#inst.coordinates = {axes[k].tag:v for k,v in coordinates.items()}
		fvar.instances.append(inst)

	assert "fvar" not in font
	font['fvar'] = fvar

	return fvar

def _add_avar(font, axes):
	"""
	Add 'avar' table to font.

	axes is an ordered dictionary of AxisDescriptor objects.
	"""

	assert axes
	assert isinstance(axes, OrderedDict)

	log.info("Generating avar")

	avar = newTable('avar')

	interesting = False
	for axis in axes.values():
		# Currently, some rasterizers require that the default value maps
		# (-1 to -1, 0 to 0, and 1 to 1) be present for all the segment
		# maps, even when the default normalization mapping for the axis
		# was not modified.
		# https://github.com/googlei18n/fontmake/issues/295
		# https://github.com/fonttools/fonttools/issues/1011
		# TODO(anthrotype) revert this (and 19c4b37) when issue is fixed
		curve = avar.segments[axis.tag] = {-1.0: -1.0, 0.0: 0.0, 1.0: 1.0}
		if not axis.map:
			continue

		items = sorted(axis.map)
		keys = [item[0] for item in items]
		vals = [item[1] for item in items]

		# Current avar requirements.  We don't have to enforce
		# these on the designer and can deduce some ourselves,
		# but for now just enforce them.
		assert axis.minimum == min(keys)
		assert axis.maximum == max(keys)
		assert axis.default in keys
		# No duplicates
		assert len(set(keys)) == len(keys)
		assert len(set(vals)) == len(vals)
		# Ascending values
		assert sorted(vals) == vals

		keys_triple = (axis.minimum, axis.default, axis.maximum)
		vals_triple = tuple(axis.map_forward(v) for v in keys_triple)

		keys = [models.normalizeValue(v, keys_triple) for v in keys]
		vals = [models.normalizeValue(v, vals_triple) for v in vals]

		if all(k == v for k, v in zip(keys, vals)):
			continue
		interesting = True

		curve.update(zip(keys, vals))

		assert 0.0 in curve and curve[0.0] == 0.0
		assert -1.0 not in curve or curve[-1.0] == -1.0
		assert +1.0 not in curve or curve[+1.0] == +1.0
		# curve.update({-1.0: -1.0, 0.0: 0.0, 1.0: 1.0})

	assert "avar" not in font
	if not interesting:
		log.info("No need for avar")
		avar = None
	else:
		font['avar'] = avar

	return avar

def _add_stat(font, axes):
	# for now we just get the axis tags and nameIDs from the fvar,
	# so we can reuse the same nameIDs which were defined in there.
	# TODO make use of 'axes' once it adds style attributes info:
	# https://github.com/LettError/designSpaceDocument/issues/8

	if "STAT" in font:
		return

	fvarTable = font['fvar']

	STAT = font["STAT"] = newTable('STAT')
	stat = STAT.table = ot.STAT()
	stat.Version = 0x00010001

	axisRecords = []
	for i, a in enumerate(fvarTable.axes):
		axis = ot.AxisRecord()
		axis.AxisTag = Tag(a.axisTag)
		axis.AxisNameID = a.axisNameID
		axis.AxisOrdering = i
		axisRecords.append(axis)

	axisRecordArray = ot.AxisRecordArray()
	axisRecordArray.Axis = axisRecords
	# XXX these should not be hard-coded but computed automatically
	stat.DesignAxisRecordSize = 8
	stat.DesignAxisCount = len(axisRecords)
	stat.DesignAxisRecord = axisRecordArray

	# for the elided fallback name, we default to the base style name.
	# TODO make this user-configurable via designspace document
	stat.ElidedFallbackNameID = 2


def _get_phantom_points(font, glyphName, defaultVerticalOrigin=None):
	glyf = font["glyf"]
	glyph = glyf[glyphName]
	horizontalAdvanceWidth, leftSideBearing = font["hmtx"].metrics[glyphName]
	if not hasattr(glyph, 'xMin'):
		glyph.recalcBounds(glyf)
	leftSideX = glyph.xMin - leftSideBearing
	rightSideX = leftSideX + horizontalAdvanceWidth
	if "vmtx" in font:
		verticalAdvanceWidth, topSideBearing = font["vmtx"].metrics[glyphName]
		topSideY = topSideBearing + glyph.yMax
	else:
		# without vmtx, use ascent as vertical origin and UPEM as vertical advance
		# like HarfBuzz does
		verticalAdvanceWidth = font["head"].unitsPerEm
		try:
			topSideY = font["hhea"].ascent
		except KeyError:
			# sparse masters may not contain an hhea table; use the ascent
			# of the default master as the vertical origin
			assert defaultVerticalOrigin is not None
			topSideY = defaultVerticalOrigin
	bottomSideY = topSideY - verticalAdvanceWidth
	return [
		(leftSideX, 0),
		(rightSideX, 0),
		(0, topSideY),
		(0, bottomSideY),
	]


# TODO Move to glyf or gvar table proper
def _GetCoordinates(font, glyphName, defaultVerticalOrigin=None):
	"""font, glyphName --> glyph coordinates as expected by "gvar" table

	The result includes four "phantom points" for the glyph metrics,
	as mandated by the "gvar" spec.
	"""
	glyf = font["glyf"]
	if glyphName not in glyf.glyphs: return None
	glyph = glyf[glyphName]
	if glyph.isComposite():
		coord = GlyphCoordinates([(getattr(c, 'x', 0),getattr(c, 'y', 0)) for c in glyph.components])
		control = (glyph.numberOfContours,[c.glyphName for c in glyph.components])
	else:
		allData = glyph.getCoordinates(glyf)
		coord = allData[0]
		control = (glyph.numberOfContours,)+allData[1:]

	# Add phantom points for (left, right, top, bottom) positions.
	phantomPoints = _get_phantom_points(font, glyphName, defaultVerticalOrigin)
	coord = coord.copy()
	coord.extend(phantomPoints)

	return coord, control

# TODO Move to glyf or gvar table proper
def _SetCoordinates(font, glyphName, coord):
	glyf = font["glyf"]
	assert glyphName in glyf.glyphs
	glyph = glyf[glyphName]

	# Handle phantom points for (left, right, top, bottom) positions.
	assert len(coord) >= 4
	if not hasattr(glyph, 'xMin'):
		glyph.recalcBounds(glyf)
	leftSideX = coord[-4][0]
	rightSideX = coord[-3][0]
	topSideY = coord[-2][1]
	bottomSideY = coord[-1][1]

	for _ in range(4):
		del coord[-1]

	if glyph.isComposite():
		assert len(coord) == len(glyph.components)
		for p,comp in zip(coord, glyph.components):
			if hasattr(comp, 'x'):
				comp.x,comp.y = p
	elif glyph.numberOfContours is 0:
		assert len(coord) == 0
	else:
		assert len(coord) == len(glyph.coordinates)
		glyph.coordinates = coord

	glyph.recalcBounds(glyf)

	horizontalAdvanceWidth = otRound(rightSideX - leftSideX)
	if horizontalAdvanceWidth < 0:
		# unlikely, but it can happen, see:
		# https://github.com/fonttools/fonttools/pull/1198
		horizontalAdvanceWidth = 0
	leftSideBearing = otRound(glyph.xMin - leftSideX)
	# XXX Handle vertical
	font["hmtx"].metrics[glyphName] = horizontalAdvanceWidth, leftSideBearing

def _add_gvar(font, masterModel, master_ttfs, tolerance=0.5, optimize=True):

	assert tolerance >= 0

	log.info("Generating gvar")
	assert "gvar" not in font
	gvar = font["gvar"] = newTable('gvar')
	gvar.version = 1
	gvar.reserved = 0
	gvar.variations = {}

	glyf = font['glyf']

	# use hhea.ascent of base master as default vertical origin when vmtx is missing
	defaultVerticalOrigin = font['hhea'].ascent
	for glyph in font.getGlyphOrder():

		isComposite = glyf[glyph].isComposite()

		allData = [
			_GetCoordinates(m, glyph, defaultVerticalOrigin=defaultVerticalOrigin)
			for m in master_ttfs
		]
		model, allData = masterModel.getSubModel(allData)

		allCoords = [d[0] for d in allData]
		allControls = [d[1] for d in allData]
		control = allControls[0]
		if not models.allEqual(allControls):
			log.warning("glyph %s has incompatible masters; skipping" % glyph)
			continue
		del allControls

		# Update gvar
		gvar.variations[glyph] = []
		deltas = model.getDeltas(allCoords)
		supports = model.supports
		assert len(deltas) == len(supports)

		# Prepare for IUP optimization
		origCoords = deltas[0]
		endPts = control[1] if control[0] >= 1 else list(range(len(control[1])))

		for i,(delta,support) in enumerate(zip(deltas[1:], supports[1:])):
			if all(abs(v) <= tolerance for v in delta.array) and not isComposite:
				continue
			var = TupleVariation(support, delta)
			if optimize:
				delta_opt = iup_delta_optimize(delta, origCoords, endPts, tolerance=tolerance)

				if None in delta_opt:
					"""In composite glyphs, there should be one 0 entry
					to make sure the gvar entry is written to the font.

					This is to work around an issue with macOS 10.14 and can be
					removed once the behaviour of macOS is changed.

					https://github.com/fonttools/fonttools/issues/1381
					"""
					if all(d is None for d in delta_opt):
						delta_opt = [(0, 0)] + [None] * (len(delta_opt) - 1)
					# Use "optimized" version only if smaller...
					var_opt = TupleVariation(support, delta_opt)

					axis_tags = sorted(support.keys()) # Shouldn't matter that this is different from fvar...?
					tupleData, auxData, _ = var.compile(axis_tags, [], None)
					unoptimized_len = len(tupleData) + len(auxData)
					tupleData, auxData, _ = var_opt.compile(axis_tags, [], None)
					optimized_len = len(tupleData) + len(auxData)

					if optimized_len < unoptimized_len:
						var = var_opt

			gvar.variations[glyph].append(var)

def _remove_TTHinting(font):
	for tag in ("cvar", "cvt ", "fpgm", "prep"):
		if tag in font:
			del font[tag]
	for attr in ("maxTwilightPoints", "maxStorage", "maxFunctionDefs", "maxInstructionDefs", "maxStackElements", "maxSizeOfInstructions"):
		setattr(font["maxp"], attr, 0)
	font["maxp"].maxZones = 1
	font["glyf"].removeHinting()
	# TODO: Modify gasp table to deactivate gridfitting for all ranges?

def _merge_TTHinting(font, masterModel, master_ttfs, tolerance=0.5):

	log.info("Merging TT hinting")
	assert "cvar" not in font

	# Check that the existing hinting is compatible

	# fpgm and prep table

	for tag in ("fpgm", "prep"):
		all_pgms = [m[tag].program for m in master_ttfs if tag in m]
		if len(all_pgms) == 0:
			continue
		if tag in font:
			font_pgm = font[tag].program
		else:
			font_pgm = Program()
		if any(pgm != font_pgm for pgm in all_pgms):
			log.warning("Masters have incompatible %s tables, hinting is discarded." % tag)
			_remove_TTHinting(font)
			return

	# glyf table

	for name, glyph in font["glyf"].glyphs.items():
		all_pgms = [
			m["glyf"][name].program
			for m in master_ttfs
			if name in m['glyf'] and hasattr(m["glyf"][name], "program")
		]
		if not any(all_pgms):
			continue
		glyph.expand(font["glyf"])
		if hasattr(glyph, "program"):
			font_pgm = glyph.program
		else:
			font_pgm = Program()
		if any(pgm != font_pgm for pgm in all_pgms if pgm):
			log.warning("Masters have incompatible glyph programs in glyph '%s', hinting is discarded." % name)
			# TODO Only drop hinting from this glyph.
			_remove_TTHinting(font)
			return

	# cvt table

	all_cvs = [Vector(m["cvt "].values) if 'cvt ' in m else None
		   for m in master_ttfs]

	nonNone_cvs = models.nonNone(all_cvs)
	if not nonNone_cvs:
		# There is no cvt table to make a cvar table from, we're done here.
		return

	if not models.allEqual(len(c) for c in nonNone_cvs):
		log.warning("Masters have incompatible cvt tables, hinting is discarded.")
		_remove_TTHinting(font)
		return

	# We can build the cvar table now.

	cvar = font["cvar"] = newTable('cvar')
	cvar.version = 1
	cvar.variations = []

	deltas, supports = masterModel.getDeltasAndSupports(all_cvs)
	for i,(delta,support) in enumerate(zip(deltas[1:], supports[1:])):
		delta = [otRound(d) for d in delta]
		if all(abs(v) <= tolerance for v in delta):
			continue
		var = TupleVariation(support, delta)
		cvar.variations.append(var)

def _add_HVAR(font, masterModel, master_ttfs, axisTags):

	log.info("Generating HVAR")

	glyphOrder = font.getGlyphOrder()

	hAdvanceDeltasAndSupports = {}
	metricses = [m["hmtx"].metrics for m in master_ttfs]
	for glyph in glyphOrder:
		hAdvances = [metrics[glyph][0] if glyph in metrics else None for metrics in metricses]
		hAdvanceDeltasAndSupports[glyph] = masterModel.getDeltasAndSupports(hAdvances)

	singleModel = models.allEqual(id(v[1]) for v in hAdvanceDeltasAndSupports.values())

	directStore = None
	if singleModel:
		# Build direct mapping

		supports = next(iter(hAdvanceDeltasAndSupports.values()))[1][1:]
		varTupleList = builder.buildVarRegionList(supports, axisTags)
		varTupleIndexes = list(range(len(supports)))
		varData = builder.buildVarData(varTupleIndexes, [], optimize=False)
		for glyphName in glyphOrder:
			varData.addItem(hAdvanceDeltasAndSupports[glyphName][0])
		varData.optimize()
		directStore = builder.buildVarStore(varTupleList, [varData])

	# Build optimized indirect mapping
	storeBuilder = varStore.OnlineVarStoreBuilder(axisTags)
	mapping = {}
	for glyphName in glyphOrder:
		deltas,supports = hAdvanceDeltasAndSupports[glyphName]
		storeBuilder.setSupports(supports)
		mapping[glyphName] = storeBuilder.storeDeltas(deltas)
	indirectStore = storeBuilder.finish()
	mapping2 = indirectStore.optimize()
	mapping = [mapping2[mapping[g]] for g in glyphOrder]
	advanceMapping = builder.buildVarIdxMap(mapping, glyphOrder)

	use_direct = False
	if directStore:
		# Compile both, see which is more compact

		writer = OTTableWriter()
		directStore.compile(writer, font)
		directSize = len(writer.getAllData())

		writer = OTTableWriter()
		indirectStore.compile(writer, font)
		advanceMapping.compile(writer, font)
		indirectSize = len(writer.getAllData())

		use_direct = directSize < indirectSize

	# Done; put it all together.
	assert "HVAR" not in font
	HVAR = font["HVAR"] = newTable('HVAR')
	hvar = HVAR.table = ot.HVAR()
	hvar.Version = 0x00010000
	hvar.LsbMap = hvar.RsbMap = None
	if use_direct:
		hvar.VarStore = directStore
		hvar.AdvWidthMap = None
	else:
		hvar.VarStore = indirectStore
		hvar.AdvWidthMap = advanceMapping

def _add_MVAR(font, masterModel, master_ttfs, axisTags):

	log.info("Generating MVAR")

	store_builder = varStore.OnlineVarStoreBuilder(axisTags)

	records = []
	lastTableTag = None
	fontTable = None
	tables = None
	# HACK: we need to special-case post.underlineThickness and .underlinePosition
	# and unilaterally/arbitrarily define a sentinel value to distinguish the case
	# when a post table is present in a given master simply because that's where
	# the glyph names in TrueType must be stored, but the underline values are not
	# meant to be used for building MVAR's deltas. The value of -0x8000 (-36768)
	# the minimum FWord (int16) value, was chosen for its unlikelyhood to appear
	# in real-world underline position/thickness values.
	specialTags = {"unds": -0x8000, "undo": -0x8000}

	for tag, (tableTag, itemName) in sorted(MVAR_ENTRIES.items(), key=lambda kv: kv[1]):
		# For each tag, fetch the associated table from all fonts (or not when we are
		# still looking at a tag from the same tables) and set up the variation model
		# for them.
		if tableTag != lastTableTag:
			tables = fontTable = None
			if tableTag in font:
				fontTable = font[tableTag]
				tables = []
				for master in master_ttfs:
					if tableTag not in master or (
						tag in specialTags
						and getattr(master[tableTag], itemName) == specialTags[tag]
					):
						tables.append(None)
					else:
						tables.append(master[tableTag])
				model, tables = masterModel.getSubModel(tables)
				store_builder.setModel(model)
			lastTableTag = tableTag

		if tables is None:  # Tag not applicable to the master font.
			continue

		# TODO support gasp entries

		master_values = [getattr(table, itemName) for table in tables]
		if models.allEqual(master_values):
			base, varIdx = master_values[0], None
		else:
			base, varIdx = store_builder.storeMasters(master_values)
		setattr(fontTable, itemName, base)

		if varIdx is None:
			continue
		log.info('	%s: %s.%s	%s', tag, tableTag, itemName, master_values)
		rec = ot.MetricsValueRecord()
		rec.ValueTag = tag
		rec.VarIdx = varIdx
		records.append(rec)

	assert "MVAR" not in font
	if records:
		store = store_builder.finish()
		# Optimize
		mapping = store.optimize()
		for rec in records:
			rec.VarIdx = mapping[rec.VarIdx]

		MVAR = font["MVAR"] = newTable('MVAR')
		mvar = MVAR.table = ot.MVAR()
		mvar.Version = 0x00010000
		mvar.Reserved = 0
		mvar.VarStore = store
		# XXX these should not be hard-coded but computed automatically
		mvar.ValueRecordSize = 8
		mvar.ValueRecordCount = len(records)
		mvar.ValueRecord = sorted(records, key=lambda r: r.ValueTag)


def _merge_OTL(font, model, master_fonts, axisTags):

	log.info("Merging OpenType Layout tables")
	merger = VariationMerger(model, axisTags, font)

	merger.mergeTables(font, master_fonts, ['GSUB', 'GDEF', 'GPOS'])
	store = merger.store_builder.finish()
	if not store.VarData:
		return
	try:
		GDEF = font['GDEF'].table
		assert GDEF.Version <= 0x00010002
	except KeyError:
		font['GDEF']= newTable('GDEF')
		GDEFTable = font["GDEF"] = newTable('GDEF')
		GDEF = GDEFTable.table = ot.GDEF()
	GDEF.Version = 0x00010003
	GDEF.VarStore = store

	# Optimize
	varidx_map = store.optimize()
	GDEF.remap_device_varidxes(varidx_map)
	if 'GPOS' in font:
		font['GPOS'].table.remap_device_varidxes(varidx_map)


def _add_GSUB_feature_variations(font, axes, internal_axis_supports, rules):

	def normalize(name, value):
		return models.normalizeLocation(
			{name: value}, internal_axis_supports
		)[name]

	log.info("Generating GSUB FeatureVariations")

	axis_tags = {name: axis.tag for name, axis in axes.items()}

	conditional_subs = []
	for rule in rules:

		region = []
		for conditions in rule.conditionSets:
			space = {}
			for condition in conditions:
				axis_name = condition["name"]
				if condition["minimum"] is not None:
					minimum = normalize(axis_name, condition["minimum"])
				else:
					minimum = -1.0
				if condition["maximum"] is not None:
					maximum = normalize(axis_name, condition["maximum"])
				else:
					maximum = 1.0
				tag = axis_tags[axis_name]
				space[tag] = (minimum, maximum)
			region.append(space)

		subs = {k: v for k, v in rule.subs}

		conditional_subs.append((region, subs))

	addFeatureVariations(font, conditional_subs)


_DesignSpaceData = namedtuple(
	"_DesignSpaceData",
	[
		"axes",
		"internal_axis_supports",
		"base_idx",
		"normalized_master_locs",
		"masters",
		"instances",
		"rules",
	],
)


def _add_CFF2(varFont, model, master_fonts):
	from .cff import (convertCFFtoCFF2, merge_region_fonts)
	glyphOrder = varFont.getGlyphOrder()
	convertCFFtoCFF2(varFont)
	ordered_fonts_list = model.reorderMasters(master_fonts, model.reverseMapping)
	# re-ordering the master list simplifies building the CFF2 data item lists.
	merge_region_fonts(varFont, model, ordered_fonts_list, glyphOrder)


def load_designspace(designspace):
	# TODO: remove this and always assume 'designspace' is a DesignSpaceDocument,
	# never a file path, as that's already handled by caller
	if hasattr(designspace, "sources"):  # Assume a DesignspaceDocument
		ds = designspace
	else:  # Assume a file path
		ds = DesignSpaceDocument.fromfile(designspace)

	masters = ds.sources
	if not masters:
		raise VarLibError("no sources found in .designspace")
	instances = ds.instances

	standard_axis_map = OrderedDict([
		('weight',  ('wght', {'en': u'Weight'})),
		('width',   ('wdth', {'en': u'Width'})),
		('slant',   ('slnt', {'en': u'Slant'})),
		('optical', ('opsz', {'en': u'Optical Size'})),
		('italic',  ('ital', {'en': u'Italic'})),
		])

	# Setup axes
	axes = OrderedDict()
	for axis in ds.axes:
		axis_name = axis.name
		if not axis_name:
			assert axis.tag is not None
			axis_name = axis.name = axis.tag

		if axis_name in standard_axis_map:
			if axis.tag is None:
				axis.tag = standard_axis_map[axis_name][0]
			if not axis.labelNames:
				axis.labelNames.update(standard_axis_map[axis_name][1])
		else:
			assert axis.tag is not None
			if not axis.labelNames:
				axis.labelNames["en"] = tounicode(axis_name)

		axes[axis_name] = axis
	log.info("Axes:\n%s", pformat([axis.asdict() for axis in axes.values()]))

	# Check all master and instance locations are valid and fill in defaults
	for obj in masters+instances:
		obj_name = obj.name or obj.styleName or ''
		loc = obj.location
		for axis_name in loc.keys():
			assert axis_name in axes, "Location axis '%s' unknown for '%s'." % (axis_name, obj_name)
		for axis_name,axis in axes.items():
			if axis_name not in loc:
				loc[axis_name] = axis.default
			else:
				v = axis.map_backward(loc[axis_name])
				assert axis.minimum <= v <= axis.maximum, "Location for axis '%s' (mapped to %s) out of range for '%s' [%s..%s]" % (axis_name, v, obj_name, axis.minimum, axis.maximum)

	# Normalize master locations

	internal_master_locs = [o.location for o in masters]
	log.info("Internal master locations:\n%s", pformat(internal_master_locs))

	# TODO This mapping should ideally be moved closer to logic in _add_fvar/avar
	internal_axis_supports = {}
	for axis in axes.values():
		triple = (axis.minimum, axis.default, axis.maximum)
		internal_axis_supports[axis.name] = [axis.map_forward(v) for v in triple]
	log.info("Internal axis supports:\n%s", pformat(internal_axis_supports))

	normalized_master_locs = [models.normalizeLocation(m, internal_axis_supports) for m in internal_master_locs]
	log.info("Normalized master locations:\n%s", pformat(normalized_master_locs))

	# Find base master
	base_idx = None
	for i,m in enumerate(normalized_master_locs):
		if all(v == 0 for v in m.values()):
			assert base_idx is None
			base_idx = i
	assert base_idx is not None, "Base master not found; no master at default location?"
	log.info("Index of base master: %s", base_idx)

	return _DesignSpaceData(
		axes,
		internal_axis_supports,
		base_idx,
		normalized_master_locs,
		masters,
		instances,
		ds.rules,
	)


def build(designspace, master_finder=lambda s:s, exclude=[], optimize=True):
	"""
	Build variation font from a designspace file.

	If master_finder is set, it should be a callable that takes master
	filename as found in designspace file and map it to master font
	binary as to be opened (eg. .ttf or .otf).
	"""
	if hasattr(designspace, "sources"):  # Assume a DesignspaceDocument
		pass
	else:  # Assume a file path
		designspace = DesignSpaceDocument.fromfile(designspace)

	ds = load_designspace(designspace)
	log.info("Building variable font")

	log.info("Loading master fonts")
	master_fonts = load_masters(designspace, master_finder)

	# TODO: 'master_ttfs' is unused except for return value, remove later
	master_ttfs = []
	for master in master_fonts:
		try:
			master_ttfs.append(master.reader.file.name)
		except AttributeError:
			master_ttfs.append(None)  # in-memory fonts have no path

	# Copy the base master to work from it
	vf = deepcopy(master_fonts[ds.base_idx])

	# TODO append masters as named-instances as well; needs .designspace change.
	fvar = _add_fvar(vf, ds.axes, ds.instances)
	if 'STAT' not in exclude:
		_add_stat(vf, ds.axes)
	if 'avar' not in exclude:
		_add_avar(vf, ds.axes)

	# Map from axis names to axis tags...
	normalized_master_locs = [
		{ds.axes[k].tag: v for k,v in loc.items()} for loc in ds.normalized_master_locs
	]
	# From here on, we use fvar axes only
	axisTags = [axis.axisTag for axis in fvar.axes]

	# Assume single-model for now.
	model = models.VariationModel(normalized_master_locs, axisOrder=axisTags)
	assert 0 == model.mapping[ds.base_idx]

	log.info("Building variations tables")
	if 'MVAR' not in exclude:
		_add_MVAR(vf, model, master_fonts, axisTags)
	if 'HVAR' not in exclude:
		_add_HVAR(vf, model, master_fonts, axisTags)
	if 'GDEF' not in exclude or 'GPOS' not in exclude:
		_merge_OTL(vf, model, master_fonts, axisTags)
	if 'gvar' not in exclude and 'glyf' in vf:
		_add_gvar(vf, model, master_fonts, optimize=optimize)
	if 'cvar' not in exclude and 'glyf' in vf:
		_merge_TTHinting(vf, model, master_fonts)
	if 'GSUB' not in exclude and ds.rules:
		_add_GSUB_feature_variations(vf, ds.axes, ds.internal_axis_supports, ds.rules)
	if 'CFF2' not in exclude and 'CFF ' in vf:
		_add_CFF2(vf, model, master_fonts)

	for tag in exclude:
		if tag in vf:
			del vf[tag]

	# TODO: Only return vf for 4.0+, the rest is unused.
	return vf, model, master_ttfs


def _open_font(path, master_finder):
	# load TTFont masters from given 'path': this can be either a .TTX or an
	# OpenType binary font; or if neither of these, try use the 'master_finder'
	# callable to resolve the path to a valid .TTX or OpenType font binary.
	from fontTools.ttx import guessFileType

	master_path = os.path.normpath(path)
	tp = guessFileType(master_path)
	if tp is None:
		# not an OpenType binary/ttx, fall back to the master finder.
		master_path = master_finder(master_path)
		tp = guessFileType(master_path)
	if tp in ("TTX", "OTX"):
		font = TTFont()
		font.importXML(master_path)
	elif tp in ("TTF", "OTF", "WOFF", "WOFF2"):
		font = TTFont(master_path)
	else:
		raise VarLibError("Invalid master path: %r" % master_path)
	return font


def load_masters(designspace, master_finder=lambda s: s):
	"""Ensure that all SourceDescriptor.font attributes have an appropriate TTFont
	object loaded, or else open TTFont objects from the SourceDescriptor.path
	attributes.

	The paths can point to either an OpenType font, a TTX file, or a UFO. In the
	latter case, use the provided master_finder callable to map from UFO paths to
	the respective master font binaries (e.g. .ttf, .otf or .ttx).

	Return list of master TTFont objects in the same order they are listed in the
	DesignSpaceDocument.
	"""
	master_fonts = []

	for master in designspace.sources:
		# 1. If the caller already supplies a TTFont for a source, just take it.
		if master.font:
			font = master.font
			master_fonts.append(font)
		else:
			# If a SourceDescriptor has a layer name, demand that the compiled TTFont
			# be supplied by the caller. This spares us from modifying MasterFinder.
			if master.layerName:
				raise AttributeError(
					"Designspace source '%s' specified a layer name but lacks the "
					"required TTFont object in the 'font' attribute."
					% (master.name or "<Unknown>")
			)
			else:
				if master.path is None:
					raise AttributeError(
						"Designspace source '%s' has neither 'font' nor 'path' "
						"attributes" % (master.name or "<Unknown>")
					)
				# 2. A SourceDescriptor's path might point an OpenType binary, a
				# TTX file, or another source file (e.g. UFO), in which case we
				# resolve the path using 'master_finder' function
				master.font = font = _open_font(master.path, master_finder)
				master_fonts.append(font)

	return master_fonts


class MasterFinder(object):

	def __init__(self, template):
		self.template = template

	def __call__(self, src_path):
		fullname = os.path.abspath(src_path)
		dirname, basename = os.path.split(fullname)
		stem, ext = os.path.splitext(basename)
		path = self.template.format(
			fullname=fullname,
			dirname=dirname,
			basename=basename,
			stem=stem,
			ext=ext,
		)
		return os.path.normpath(path)


def main(args=None):
	from argparse import ArgumentParser
	from fontTools import configLogger

	parser = ArgumentParser(prog='varLib')
	parser.add_argument('designspace')
	parser.add_argument(
		'-o',
		metavar='OUTPUTFILE',
		dest='outfile',
		default=None,
		help='output file'
	)
	parser.add_argument(
		'-x',
		metavar='TAG',
		dest='exclude',
		action='append',
		default=[],
		help='exclude table'
	)
	parser.add_argument(
		'--disable-iup',
		dest='optimize',
		action='store_false',
		help='do not perform IUP optimization'
	)
	parser.add_argument(
		'--master-finder',
		default='master_ttf_interpolatable/{stem}.ttf',
		help=(
			'templated string used for finding binary font '
			'files given the source file names defined in the '
			'designspace document. The following special strings '
			'are defined: {fullname} is the absolute source file '
			'name; {basename} is the file name without its '
			'directory; {stem} is the basename without the file '
			'extension; {ext} is the source file extension; '
			'{dirname} is the directory of the absolute file '
			'name. The default value is "%(default)s".'
		)
	)
	options = parser.parse_args(args)

	# TODO: allow user to configure logging via command-line options
	configLogger(level="INFO")

	designspace_filename = options.designspace
	finder = MasterFinder(options.master_finder)
	outfile = options.outfile
	if outfile is None:
		outfile = os.path.splitext(designspace_filename)[0] + '-VF.ttf'

	vf, _, _ = build(
		designspace_filename,
		finder,
		exclude=options.exclude,
		optimize=options.optimize
	)

	log.info("Saving variation font %s", outfile)
	vf.save(outfile)


if __name__ == "__main__":
	import sys
	if len(sys.argv) > 1:
		sys.exit(main())
	import doctest
	sys.exit(doctest.testmod().failed)
