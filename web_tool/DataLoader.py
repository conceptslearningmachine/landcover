import os

import numpy as np
from enum import Enum

from urllib.request import urlopen

import fiona
import fiona.transform
import fiona.crs

import shapely
import shapely.geometry

import rasterio
import rasterio.warp
import rasterio.crs
import rasterio.io
import rasterio.mask
import rasterio.transform
import rasterio.merge

import rtree

import mercantile

import cv2
import pickle

from . import ROOT_DIR
from .DataLoaderAbstract import DataLoader

# ------------------------------------------------------
# Miscellaneous methods
# ------------------------------------------------------

def extent_to_transformed_geom(extent, dest_crs):
    left, right = extent["xmin"], extent["xmax"]
    top, bottom = extent["ymax"], extent["ymin"]

    geom = {
        "type": "Polygon",
        "coordinates": [[(left, top), (right, top), (right, bottom), (left, bottom), (left, top)]]
    }

    src_crs = extent["crs"]
    return fiona.transform.transform_geom(src_crs, dest_crs, geom)


def warp_data_to_3857(src_img, src_crs, src_transform, src_bounds):
    ''' Assume that src_img is (height, width, channels)
    '''
    assert len(src_img.shape) == 3
    src_height, src_width, num_channels = src_img.shape

    src_img_tmp = np.rollaxis(src_img.copy(), 2, 0)

    x_res, y_res = src_transform[0], -src_transform[4]

    dst_crs = rasterio.crs.CRS.from_epsg(3857)
    dst_bounds = rasterio.warp.transform_bounds(src_crs, dst_crs, *src_bounds)
    dst_transform, width, height = rasterio.warp.calculate_default_transform(
        src_crs,
        dst_crs,
        width=src_width, height=src_height,
        left=src_bounds[0],
        bottom=src_bounds[1],
        right=src_bounds[2],
        top=src_bounds[3],
        resolution=(30, 30)  # resolution in meters of the imagery data
        # resolution=(x_res, y_res) # TODO: we use the resolution of the src_input, while this parameter needs the resolution of the destination. This will break if src_crs units are degrees instead of meters
    )

    print(f'--- DataLoader, src_crs is {src_crs}. width is {width}, height is {height}')
    dst_image = np.zeros((num_channels, height, width), np.float32)
    dst_image, dst_transform = rasterio.warp.reproject(
        source=src_img_tmp,
        destination=dst_image,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        resampling=rasterio.warp.Resampling.nearest
    )
    dst_image = np.rollaxis(dst_image, 0, 3)
    
    return dst_image, dst_crs, dst_transform, dst_bounds


def crop_data_by_extent(src_img, src_crs, src_transform, extent):
    geom = extent_to_transformed_geom(extent, "epsg:3857") # TODO: hardcoded epsg:3857 because that's the only CRS that the frontend uses
    return crop_data_by_geometry(src_img, src_crs, src_transform, geom, "epsg:3857")


def crop_data_by_geometry(src_img, src_crs, src_transform, geometry, geometry_crs):
    src_img_tmp = np.rollaxis(src_img.copy(), 2, 0)
    
    geometry = fiona.transform.transform_geom(geometry_crs, str(src_crs), geometry)

    height, width, num_channels = src_img.shape
    dummy_src_profile = {
        'driver': 'GTiff', 'dtype': str(src_img.dtype),
        'width': width, 'height': height, 'count': num_channels,
        'crs': src_crs,
        'transform': src_transform
    }
    
    test_f = rasterio.io.MemoryFile()
    with test_f.open(**dummy_src_profile) as test_d:
        test_d.write(src_img_tmp)
    test_f.seek(0)
    with test_f.open() as test_d:
        dst_img, dst_transform = rasterio.mask.mask(test_d, [geometry], crop=True, all_touched=True, pad=False)
    test_f.close()
    
    dst_img = np.rollaxis(dst_img, 0, 3)
    
    return dst_img, dst_transform


# ------------------------------------------------------
# DataLoader for arbitrary GeoTIFFs
# ------------------------------------------------------
class DataLoaderCustom(DataLoader):

    @property
    def shapes(self):
        return self._shapes
    @shapes.setter
    def shapes(self, value):
        self._shapes = value

    @property
    def padding(self):
        return self._padding
    @padding.setter
    def padding(self, value):
        self._padding = value

    def __init__(self, data_fn, shapes, padding):
        self.data_fn = data_fn
        self._shapes = shapes
        self._padding = padding

    def get_data_from_extent(self, extent):
        f = rasterio.open(self.data_fn, "r")
        src_crs = f.crs
        transformed_geom = extent_to_transformed_geom(extent, f.crs.to_dict())
        transformed_geom = shapely.geometry.shape(transformed_geom)
        buffed_geom = transformed_geom.buffer(self.padding)
        geom = shapely.geometry.mapping(shapely.geometry.box(*buffed_geom.bounds))
        src_image, src_transform = rasterio.mask.mask(f, [geom], crop=True, all_touched=True, pad=False)
        f.close()

        src_image = np.rollaxis(src_image, 0, 3)

        return src_image, src_crs, src_transform, buffed_geom.bounds

    def get_area_from_shape_by_extent(self, extent, shape_layer):
        i, shape = self.get_shape_by_extent(extent, shape_layer)
        return self.shapes[shape_layer]["areas"][i]

    def get_shape_by_extent(self, extent, shape_layer):
        transformed_geom = extent_to_transformed_geom(extent, self.shapes[shape_layer]["crs"])
        transformed_shape = shapely.geometry.shape(transformed_geom)
        mask_geom = None
        for i, shape in enumerate(self.shapes[shape_layer]["geoms"]):
            if shape.contains(transformed_shape.centroid):
                return i, shape
        raise ValueError("No shape contains the centroid")

    def get_data_from_shape(self, shape):
        '''Note: We assume that the shape is a geojson geometry in EPSG:4326'''

        f = rasterio.open(self.data_fn, "r")
        src_profile = f.profile
        src_crs = f.crs.to_string()
        transformed_mask_geom = fiona.transform.transform_geom("epsg:4326", src_crs, shape)
        src_image, src_transform = rasterio.mask.mask(f, [transformed_mask_geom], crop=True, all_touched=True, pad=False)
        f.close()

        src_image = np.rollaxis(src_image, 0, 3)
        return src_image, src_profile, src_transform, shapely.geometry.shape(transformed_mask_geom).bounds, src_crs


# ------------------------------------------------------
# DataLoader for US NAIP data and other aligned layers
# ------------------------------------------------------
class NAIPTileIndex(object):
    TILES = None
    
    @staticmethod
    def lookup(extent):
        if NAIPTileIndex.TILES is None:
            assert all([os.path.exists(fn) for fn in [
                ROOT_DIR + "/data/tile_index.dat",
                ROOT_DIR + "/data/tile_index.idx",
                ROOT_DIR + "/data/tiles.p"
            ]]), "You do not have the correct files, did you setup the project correctly"
            NAIPTileIndex.TILES = pickle.load(open(ROOT_DIR + "/data/tiles.p", "rb"))
        return NAIPTileIndex.lookup_naip_tile_by_geom(extent)

    @staticmethod
    def lookup_naip_tile_by_geom(extent):
        tile_index = rtree.index.Index(ROOT_DIR + "/data/tile_index")

        geom = extent_to_transformed_geom(extent, "EPSG:4269")
        minx, miny, maxx, maxy = shapely.geometry.shape(geom).bounds
        geom = shapely.geometry.mapping(shapely.geometry.box(minx, miny, maxx, maxy, ccw=True))

        geom = shapely.geometry.shape(geom)
        intersected_indices = list(tile_index.intersection(geom.bounds))
        for idx in intersected_indices:
            intersected_fn = NAIPTileIndex.TILES[idx][0]
            intersected_geom = NAIPTileIndex.TILES[idx][1]
            if intersected_geom.contains(geom):
                print("Found %d intersections, returning at %s" % (len(intersected_indices), intersected_fn))
                return intersected_fn

        if len(intersected_indices) > 0:
            raise ValueError("Error, there are overlaps with tile index, but no tile completely contains selection")
        else:
            raise ValueError("No tile intersections")

class USALayerGeoDataTypes(Enum):
    NAIP = 1
    NLCD = 2
    LANDSAT_LEAFON = 3
    LANDSAT_LEAFOFF = 4
    BUILDINGS = 5
    LANDCOVER = 6

class DataLoaderUSALayer(DataLoader):

    @property
    def shapes(self):
        return self._shapes
    @shapes.setter
    def shapes(self, value):
        self._shapes = value

    @property
    def padding(self):
        return self._padding
    @padding.setter
    def padding(self, value):
        self._padding = value

    def __init__(self, shapes, padding):
        self._shapes = shapes
        self._padding = padding

    def get_fn_by_geo_data_type(self, naip_fn, geo_data_type):
        fn = None

        if geo_data_type == USALayerGeoDataTypes.NAIP:
            fn = naip_fn
        elif geo_data_type == USALayerGeoDataTypes.NLCD:
            fn = naip_fn.replace("/esri-naip/", "/resampled-nlcd/")[:-4] + "_nlcd.tif"
        elif geo_data_type == USALayerGeoDataTypes.LANDSAT_LEAFON:
            fn = naip_fn.replace("/esri-naip/data/v1/", "/resampled-landsat8/data/leaf_on/")[:-4] + "_landsat.tif"
        elif geo_data_type == USALayerGeoDataTypes.LANDSAT_LEAFOFF:
            fn = naip_fn.replace("/esri-naip/data/v1/", "/resampled-landsat8/data/leaf_off/")[:-4] + "_landsat.tif"
        elif geo_data_type == USALayerGeoDataTypes.BUILDINGS:
            fn = naip_fn.replace("/esri-naip/", "/resampled-buildings/")[:-4] + "_building.tif"
        elif geo_data_type == USALayerGeoDataTypes.LANDCOVER:
            fn = naip_fn.replace("/esri-naip/", "/resampled-lc/")[:-4] + "_lc.tif"
        else:
            raise ValueError("GeoDataType not recognized")

        return fn
    
    def get_shape_by_extent(self, extent, shape_layer):
        transformed_geom = extent_to_transformed_geom(extent, shapes_crs)
        transformed_shape = shapely.geometry.shape(transformed_geom)
        mask_geom = None
        for i, shape in enumerate(shapes):
            if shape.contains(transformed_shape.centroid):
                return i, shape
        raise ValueError("No shape contains the centroid")

    def get_data_from_extent(self, extent, geo_data_type=USALayerGeoDataTypes.NAIP):
        naip_fn = NAIPTileIndex.lookup(extent)
        fn = self.get_fn_by_geo_data_type(naip_fn, geo_data_type)

        f = rasterio.open(fn, "r")
        src_crs = f.crs
        transformed_geom = extent_to_transformed_geom(extent, f.crs.to_dict())
        transformed_geom = shapely.geometry.shape(transformed_geom)
        buffed_geom = transformed_geom.buffer(self.padding)
        geom = shapely.geometry.mapping(shapely.geometry.box(*buffed_geom.bounds))
        src_image, src_transform = rasterio.mask.mask(f, [geom], crop=True)
        f.close()

        return src_image, src_crs, src_transform, buffed_geom.bounds

    def get_area_from_shape_by_extent(self, extent, shape_layer):
        raise NotImplementedError()

    def get_data_from_shape(self, shape):
        raise NotImplementedError()

# ------------------------------------------------------
# DataLoader for loading RGB data from arbitrary basemaps
# ------------------------------------------------------
class DataLoaderBasemap(DataLoader):

    @property
    def shapes(self):
        return self._shapes
    @shapes.setter
    def shapes(self, value):
        self._shapes = value

    @property
    def padding(self):
        return self._padding
    @padding.setter
    def padding(self, value):
        self._padding = value

    def __init__(self, data_url, padding):
        self.data_url = data_url
        self._padding = padding
        self.zoom_level = 17

    def get_image_by_xyz_from_url(self, tile):
        '''NOTE: Here "tile" refers to a mercantile "Tile" object.'''
        req = urlopen(self.data_url.format(
            z=tile.z, y=tile.y, x=tile.x
        ))
        arr = np.asarray(bytearray(req.read()), dtype=np.uint8)
        img = cv2.imdecode(arr, -1)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        return img    

    def get_tile_as_virtual_raster(self, tile):
        '''NOTE: Here "tile" refers to a mercantile "Tile" object.'''
        img = self.get_image_by_xyz_from_url(tile)
        geom = shapely.geometry.shape(mercantile.feature(tile)["geometry"])
        minx, miny, maxx, maxy = geom.bounds
        dst_transform = rasterio.transform.from_bounds(minx, miny, maxx, maxy, 256, 256)
        dst_profile = {
            "driver": "GTiff",
            "width": 256,
            "height": 256,
            "transform": dst_transform,
            "crs": "epsg:4326",
            "count": 3,
            "dtype": "uint8"
        }    
        test_f = rasterio.io.MemoryFile()
        with test_f.open(**dst_profile) as test_d:
            test_d.write(img[:,:,0], 1)
            test_d.write(img[:,:,1], 2)
            test_d.write(img[:,:,2], 3)
        test_f.seek(0)
        
        return test_f

    def get_shape_by_extent(self, extent, shape_layer):
        raise NotImplementedError()

    def get_data_from_extent(self, extent):
        transformed_geom = extent_to_transformed_geom(extent, "epsg:4326")
        transformed_geom = shapely.geometry.shape(transformed_geom)
        buffed_geom = transformed_geom.buffer(self.padding)
        
        minx, miny, maxx, maxy = buffed_geom.bounds
        
        virtual_files = [] # this is nutty
        virtual_datasets = []
        for i, tile in enumerate(mercantile.tiles(minx, miny, maxx, maxy, self.zoom_level)):
            f = self.get_tile_as_virtual_raster(tile)
            virtual_files.append(f)
            virtual_datasets.append(f.open())
        out_image, out_transform = rasterio.merge.merge(virtual_datasets, bounds=(minx, miny, maxx, maxy))

        for ds in virtual_datasets:
            ds.close()
        for f in virtual_files:
            f.close()
        
        dst_crs = rasterio.crs.CRS.from_epsg(4326)
        dst_profile = {
            "driver": "GTiff",
            "width": out_image.shape[1],
            "height": out_image.shape[0],
            "transform": out_transform,
            "crs": "epsg:4326",
            "count": 3,
            "dtype": "uint8"
        }
        test_f = rasterio.io.MemoryFile()
        with test_f.open(**dst_profile) as test_d:
            test_d.write(out_image[:,:,0], 1)
            test_d.write(out_image[:,:,1], 2)
            test_d.write(out_image[:,:,2], 3)
        test_f.seek(0)
        test_f.close()

        r,g,b = out_image
        out_image = np.stack([r,g,b,r])
        
        return out_image, dst_crs, out_transform, (minx, miny, maxx, maxy)

    def get_area_from_shape_by_extent(self, extent, shape_layer):
        raise NotImplementedError()

    def get_data_from_shape(self, shape):
        raise NotImplementedError()