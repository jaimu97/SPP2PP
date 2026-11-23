# Horrible fuck-ass code but it works...

import json
import os
import sys
import time
import math
import tempfile
import traceback

from pathlib           import Path
from PySide6.QtGui     import *
from PySide6.QtWidgets import *

import substance_painter.export     as ex
import _substance_painter           as native
import substance_painter.textureset as ts
import substance_painter.layerstack as ls
import substance_painter.project    as project
import substance_painter.source     as source
import substance_painter.ui         as ui

actions       = []
layers        = []
samples       = {}
notes         = []
cur_channel   = None
cur_material  = ""
cur_folder    = ""
cur_size      = 0
cur_has_tiles = False

PAWPAINT_IMPORT_LUA = r'''local dir = ROOT_LITERAL
local project = pp.json.readFile(dir .. "/project.json")
assert(project, "Cannot locate exported project.json")

local function finishLayers(solids, samples)
    local converted = 0
    for i = 0, pp.layers.count()-1 do
        local name = pp.layers.name(i)
        local fill = solids[name]
        if fill then
            name = fill.name
            assert(pp.layers.type(i) == "Fill", "Expected fill: " .. name)
            assert(pp.fill.setChannelEnabled(i, "albedo", true) ~= false, name)
            assert(pp.fill.setAlbedo(i, fill.rgb[1], fill.rgb[2], fill.rgb[3]) ~= false, name)
            pp.layers.setName(i, name)
        end
        local value = samples[name]
        if value == 0 or value == 1 then
            for _,mask in ipairs(pp.mask.list(i)) do
                if mask.imageBacked and mask.name == "Painter mask" then
                    assert(pp.mask.fill(i, mask.id, value, 0, 0, 1, 1),
                           "Mask conversion failed: " .. name)
                    converted = converted + 1
                end
            end
        end
    end
    pp.log(tostring(converted) .. " imported masks converted to paintable")
end

local ok, why = pp.project.openMesh(dir .. "/model.fbx", {resolution=project.resolution}, function(result)
    assert(result.ok, result.error)
    assert(not pp.mesh.isMultiTile(), "This test exporter supports single-tile projects only")
    local function load(i)
        local m = project.materials[i]
        if not m then pp.log("Painter import complete. Save with File > Save As."); return end
        local index
        for n=0,pp.mesh.materialCount()-1 do
            if pp.mesh.materialName(n) == m.name then index = n; break end
        end
        if not index and #project.materials == 1 and pp.mesh.materialCount()==1 then index=0 end
        assert(index, "No matching mesh material: " .. m.name)
        local base = dir .. "/" .. m.folder .. "/"
        local manifest = assert(pp.json.readFile(base .. "manifest.json"))
        local spec = manifest.layers
        local samples = manifest.maskSamples or {}
        local solids, used = {}, {}
        for _,layer in ipairs(spec) do used[layer.name] = true end
        for n,layer in ipairs(spec) do
            if layer.solidAlbedo then
                local tag = "__PainterBridgeSolid_" .. n
                while used[tag] do tag = tag .. "_" end
                solids[tag] = {name=layer.name, rgb=layer.solidAlbedo}
                layer.name, layer.solidAlbedo = tag, nil
            end
            if layer.channels then layer.channels.albedo = base .. layer.channels.albedo end
            for _,mask in ipairs(layer.masks or {}) do mask.path = base .. mask.path end
        end
        local job, reason = pp.import.layers(index, spec, function(r)
            assert(r.ok, "Import ended: " .. r.state)
            for _,missing in ipairs(r.leftOut or {}) do error(missing.path .. ": " .. missing.why) end
            local previous = pp.layers.activeMaterial()
            pp.layers.setActiveMaterial(index)
            assert(pp.layers.activeMaterial() == index, "Cannot select imported material")
            finishLayers(solids, samples)
            pp.layers.setActiveMaterial(previous)
            load(i+1)
        end)
        assert(job, reason)
    end
    load(1)
end)
assert(ok, why)
'''


def save_png(node, index, is_mask):
    global cur_channel, cur_folder, cur_size
    if is_mask == True:
        prefix = "mask-" + str(index)
        channel = None
    else:
        prefix = "layer-" + str(index)
        channel = cur_channel
    # get the size log2
    size_log2 = 0
    if cur_size == 512:
        size_log2 = 9
    elif cur_size == 1024:
        size_log2 = 10
    elif cur_size == 2048:
        size_log2 = 11
    elif cur_size == 4096:
        size_log2 = 12
    # make the params
    params                     = {}
    params["fileFormat"]       = "png"
    params["paddingAlgorithm"] = "transparent"
    params["dilationDistance"] = 0
    params["bitDepth"]         = "8"
    params["dithering"]        = False
    params["keepAlpha"]        = not is_mask
    params["sizeLog2"]         = size_log2
    file_name = prefix + ".png"
    path = os.path.join(cur_folder, file_name)
    template = os.path.join(cur_folder, prefix + "(_$udim).png")
    # print("saving " + path)
    # do the export
    res = native.export.export_stack_node_texture(node.uid(), channel, template, json.dumps(params))
    result = ex.TextureExportResult(*res)
    if result.status != ex.ExportStatus.Success:
        raise RuntimeError(node.get_name() + " -> " + file_name + ": " + result.message)
    # looking for the file that was made
    found = []
    for f in os.listdir(cur_folder):
        if f.startswith(prefix + "_") and f.endswith(".png"):
            found.append(Path(cur_folder) / f)
    if os.path.exists(path) == True:
        found.append(Path(path))
    if len(found) != 1:
        raise RuntimeError("Expected one " + file_name + " tile; found " + str(found))
    # rename it
    if str(found[0]) != path:
        os.rename(str(found[0]), path)
    return file_name


# checks if the mask is white (1) or black (0), otherwise returns -1
# why is this even an option? white on black or black on white masks...
def check_mask(path):
    if QImage(path).isNull():
        return None
    top = QImage(path).pixelColor(0, 0).red()
    bottom = QImage(path).pixelColor(0, QImage(path).height() - 1).red()
    if top == 255 and bottom == 255:
        return 1
    elif top == 0 and bottom == 0:
        return 0
    else:
        return -1


# returns the colour if the layer is just one colour, otherwise None
def get_solid_colour(node):
    global cur_channel, cur_material, cur_has_tiles
    if not isinstance(node, ls.FillLayerNode):
        return None
    if cur_channel not in node.active_channels:
        return None
    if len(node.content_effects()) > 0:
        return None
    if node.source_mode == source.SourceMode.Material:
        return None
    # checking if all the meshes are enabled
    all_meshes = ts.TextureSet.from_name(cur_material).all_mesh_names()
    enabled = node.get_geometry_mask_enabled_meshes()
    for m in all_meshes:
        if m not in enabled:
            return None
    for m in enabled:
        if m not in all_meshes:
            return None
    if cur_has_tiles == True and len(node.get_geometry_mask_enabled_uv_tiles()) == 0:
        return None
    fill = node.get_source(cur_channel)
    if not isinstance(fill, source.SourceUniformColor):
        return None
    colour = fill.get_color()
    if colour.color_space != None:
        return list(colour.sRGB)
    return list(colour.value_raw)


# exports one layer and then its children
def do_layer(node, parent):
    global layers, samples, notes, cur_channel, cur_material, cur_folder
    # the index of this layer
    index = len(layers) + 1
    # check if it is a group
    is_group = False
    if isinstance(node, ls.GroupLayerNode):
        is_group = True
    # make the row
    row = {}
    row["name"] = node.get_name()
    row["visible"] = node.is_visible()
    if is_group == True:
        row["type"] = "Folder"
    else:
        if isinstance(node, ls.FillLayerNode):
            row["type"] = "Fill"
        else:
            row["type"] = "Paint"
    if parent != None:
        row["parent"] = parent
    # what is its blend mode
    mode = node.get_blending_mode(cur_channel).name
    row["opacity"] = node.get_opacity(cur_channel)
    if mode == "Normal" or mode == "Multiply" or mode == "Screen" or mode == "Overlay":
        row["blendMode"] = mode
    else:
        notes.append(cur_material + "/" + node.get_name() + ": " + mode + " imported as Normal")
    # check if it is a solid colour
    if get_solid_colour(node) != None:
        row["solidAlbedo"] = get_solid_colour(node)
    else:
        if is_group == False:
            try:
                image = save_png(node, index, False)
            except Exception as e:
                raise e
            row["channels"] = {"albedo": image}
    # check if it has a mask
    if node.has_mask() == True:
        try:
            image = save_png(node, index, True)
        except Exception as e:
            raise e
        value = check_mask(os.path.join(cur_folder, image))
        if value != None:
            # if there is already a layer with this name they need to be the same
            if node.get_name() in samples.keys():
                if samples[node.get_name()] == value:
                    samples[node.get_name()] = value
                else:
                    samples[node.get_name()] = -1
            else:
                samples[node.get_name()] = value
        mask = {}
        mask["path"] = image
        mask["name"] = "Painter mask"
        mask["enabled"] = node.is_mask_enabled()
        row["masks"] = [mask]
    # add the row to the list
    layers.append(row)
    # print(row)
    # child layers backwards so import is correct in the pp software
    if is_group == True:
        j = len(node.sub_layers()) - 1
        while j >= 0:
            do_layer(node.sub_layers()[j], index)
            j = j - 1


# does the whole fucking export
def export_open_project():
    global layers, samples, notes, cur_channel, cur_material, cur_folder, cur_size, cur_has_tiles
    # open the project, dummy
    if project.is_open() == False or not project.file_path() or project.is_busy() == True:
        raise ValueError("Open a saved .spp and wait for Painter to finish loading.")
    # make the folder
    src = Path(project.file_path())
    root = str(Path(tempfile.mkdtemp(prefix=src.stem + "-PawPaint-", dir=src.parent)))
    # start_time = time.time()
    try:
        # export the mesh
        result = ex.export_mesh(Path(os.path.join(root, "model.fbx")).as_posix(), ex.MeshExportOption.BaseMesh)
        if result.status != ex.ExportStatus.Success:
            raise RuntimeError(result.message)
        materials   = []
        notes       = []
        resolutions = []
        # loop over all the texture sets
        for i in range(len(ts.all_texture_sets())):
            mat = ts.all_texture_sets()[i]
            if len(mat.all_stacks()) != 1:
                raise ValueError("Layered material unsupported: " + mat.name())
            stack = mat.all_stacks()[0]
            sub = "material-" + str(i)

            # reset the globals
            layers  = []
            samples = {}
            cur_material = mat.name()
            cur_folder = os.path.join(root, sub)

            # find the channel
            cur_channel = None
            found_channel = False
            for c in stack.all_channels():
                if found_channel == False:
                    if c.name.lower() == "basecolor" or c.name.lower() == "diffuse":
                        cur_channel = c
                        found_channel = True
            if not cur_channel:
                raise ValueError("No base colour or diffuse channel in " + cur_material)
            os.mkdir(cur_folder)

            # get res
            texture_set = ts.TextureSet.from_name(cur_material)
            resolution = texture_set.get_resolution()
            cur_has_tiles = texture_set.has_uv_tiles()
            if cur_has_tiles == True:
                tiles = texture_set.all_uv_tiles()
                tiles_ok = True
                if len(tiles) == 0:
                    tiles_ok = False
                for t in tiles:
                    if t.u != 0 or t.v != 0:
                        tiles_ok = False
                if tiles_ok == False:
                    tile_numbers = []
                    for t in tiles:
                        tile_numbers.append(1001 + t.u + 10 * t.v)
                    raise ValueError(cur_material + ": only a single tile at 1001 is supported; "
                                     + "found " + str(tile_numbers))
                resolution = tiles[0].get_resolution()
            cur_size = resolution.width
            if cur_size != resolution.height:
                raise ValueError("PawPaint cannot match the source resolution " + str(cur_size))
            if cur_size != 512 and cur_size != 1024 and cur_size != 2048 and cur_size != 4096:
                raise ValueError("PawPaint cannot match the source resolution " + str(cur_size))

            # export the reference image
            if cur_channel.name == "BaseColor":
                map_name = "baseColor"
            else:
                map_name = "diffuse"
            channels = []
            channels.append({"destChannel": "R", "srcChannel": "R",
                            "srcMapType": "documentMap", "srcMapName": map_name})
            channels.append({"destChannel": "G", "srcChannel": "G",
                            "srcMapType": "documentMap", "srcMapName": map_name})
            channels.append({"destChannel": "B", "srcChannel": "B",
                            "srcMapType": "documentMap", "srcMapName": map_name})
            config = {
                "exportShaderParams":  False,
                "exportPath":          cur_folder, 
                "defaultExportPreset": "BridgeReference",
                "exportList":          [{"rootPath": cur_material}],
                "exportPresets":       [{
                    "name": "BridgeReference", 
                    "maps": [{"fileName": "reference", "channels": channels}]
                    }],
                "exportParameters": [{
                    "parameters":   {
                        "fileFormat":       "png", 
                        "bitDepth":         "8",
                        "dithering":        False,
                        "dilationDistance": 0,
                        "paddingAlgorithm": "transparent"}
                }]
            }
            reference = None
            try:
                reference = ex.export_project_textures(config)
            except ValueError as err:
                notes.append(cur_material + ": reference preset rejected: " + str(err))
            if reference != None:
                if reference.status == ex.ExportStatus.Cancelled:
                    raise RuntimeError("Export cancelled")
                if reference.status != ex.ExportStatus.Success:
                    notes.append(cur_material + ": reference image skipped: " + reference.message)

            # export the layers backwards because pawpaint starts at the bottom
            # like most people using it (not me tho)
            j = len(ls.get_root_layer_nodes(stack)) - 1
            while j >= 0:
                do_layer(ls.get_root_layer_nodes(stack)[j], None)
                j = j - 1

            # write the manifesto
            manifest = {}
            manifest["format"]      = "pawpaint-layer-bundle"
            manifest["version"]     = 1
            manifest["layers"]      = layers
            manifest["maskSamples"] = samples
            f = open(os.path.join(cur_folder, "manifest.json"), "w", encoding="utf-8")
            f.write(json.dumps(manifest, indent=2))
            f.close()

            resolutions.append(cur_size)
            mat_data           = {}
            mat_data["name"]   = mat.name()
            mat_data["folder"] = sub
            materials.append(mat_data)

        # check all the resolutions are the same
        if len(resolutions) == 0:
            raise ValueError("Expected materials with one common resolution.")
        for a in resolutions:
            for b in resolutions:
                if a != b:
                    raise ValueError("Expected materials with one common resolution.")

        # write the project json
        data               = {}
        data["materials"]  = materials
        data["resolution"] = resolutions[0]
        f = open(os.path.join(root, "project.json"), "w", encoding="utf-8")
        f.write(json.dumps(data, indent=2))
        f.close()

        # write the lua script
        # TODO: as_posix might break windows. not tested I use Arch btw
        lua = PAWPAINT_IMPORT_LUA.replace("ROOT_LITERAL", json.dumps(Path(root).as_posix(), ensure_ascii=False))
        f = open(os.path.join(root, "Import in PawPaint.lua"), "w", encoding="utf-8")
        f.write(lua)
        f.close()

        # write the notes
        text = ""
        for n in range(len(notes)):
            if n > 0:
                text = text + "\n"
            text = text + notes[n]
        if text == "":
            text = "No blend substitutions."
        f = open(os.path.join(root, "export-notes.txt"), "w", encoding="utf-8")
        f.write(text)
        f.close()
        # print("export took " + str(time.time() - start_time))
    except Exception:
        # write the error to a file
        f = open(os.path.join(root, "export-error.txt"), "w", encoding="utf-8")
        f.write(traceback.format_exc())
        f.close()
        raise RuntimeError("Export incomplete. See " + os.path.join(root, "export-error.txt"))
    return root


# called from SP when you click the button
def run():
    try:
        root = export_open_project()
        QMessageBox.information(ui.get_main_window(), "PawPaint export",
                                "In PawPaint run:\n" + os.path.join(root, "Import in PawPaint.lua"))
    except Exception as e:
        traceback.print_exc()
        QMessageBox.warning(ui.get_main_window(), "PawPaint export", str(e))


# called when the plugin starts
def start_plugin():
    # make the action
    action = QAction("Export layers to PawPaint", ui.get_main_window())
    # connect the action
    action.triggered.connect(run)
    # add the action to the file menu
    ui.add_action(ui.ApplicationMenu.File, action)
    actions.append(action)


# called when the plugin closes
def close_plugin():
    while len(actions) > 0:
        a = actions.pop()
        a.deleteLater()
