/*
 * mesh-warp.c - Optimized GEGL operation & ctypes preview/mesh core.
 *
 * BUILD (Linux / macOS):
    gcc -O3 -ftree-vectorize -fno-math-errno -fno-trapping-math -shared -fPIC -fopenmp \
        -I. mesh-warp.c -o mesh-warp.so -lm \
        $(pkg-config --cflags --libs gegl-0.4 glib-2.0)
 *
 * BUILD (Windows MinGW-w64 / MSYS2):
	gcc -O3 -shared -Wl,--export-all-symbols \
	    -I. mesh-warp.c -o mesh-warp.dll \
	    $(pkg-config --cflags --libs gegl-0.4 glib-2.0)
 */

#ifdef GEGL_PROPERTIES

property_int (cols, "Columns", 4)
  value_range (1, 256)

property_int (rows, "Rows", 4)
  value_range (1, 256)

property_string (grid_json, "Grid JSON", "")

property_int (pad_left, "Pad Left", 0)
property_int (pad_top,  "Pad Top", 0)
property_int (orig_width,  "Original Width", 0)
property_int (orig_height, "Original Height", 0)

property_string (pins_json, "Pins JSON", "")
  description ("Puppet-pin metadata for Python round-trip. Ignored by C.")

property_string (mode, "Mode", "grid")
  description ("Editing mode ('grid' or 'puppet'). Ignored by C.")

#else

#define GEGL_OP_FILTER
#define GEGL_OP_NAME     mesh_warp
#define GEGL_OP_C_FILE   "mesh-warp.c"

#include "gegl-op.h"
#include <math.h>
#include <string.h>
#include <stdint.h>
#include <stdlib.h>

#ifdef _OPENMP
#include <omp.h>
#endif

/* ==================================================================
 * Export Macro for Windows DLL & Linux/macOS
 * ================================================================== */
#ifdef _WIN32
  #define MW_EXPORT __declspec(dllexport)
#elif defined(__GNUC__) && __GNUC__ >= 4
  #define MW_EXPORT __attribute__((visibility("default")))
#else
  #define MW_EXPORT
#endif

/* ==================================================================
 * GEGL operation
 * ================================================================== */

typedef struct { gdouble x, y; } Vec2;

typedef struct {
  gint      ref_count;
  Vec2     *grid;
  gint      n;
  gint      cols, rows;
  gchar    *grid_json;
  uint64_t  grid_hash;
  gsize     grid_len;
  gdouble  *qmin_x, *qmax_x, *qmin_y, *qmax_y;
} MeshCache;

static GMutex g_cache_mutex;

static uint64_t
mw_fnv1a (const char *s, gsize len)
{
  uint64_t h = 1469598103934665603ULL;
  gsize i;

  for (i = 0; i < len; i++)
    {
      h ^= (uint8_t) s[i];
      h *= 1099511628211ULL;
    }

  return h;
}

static void
mesh_cache_unref (gpointer data)
{
  MeshCache *c = data;
  if (!c) return;
  if (g_atomic_int_dec_and_test (&c->ref_count))
    {
      g_free (c->grid);
      g_free (c->grid_json);
      g_free (c->qmin_x);
      g_free (c->qmax_x);
      g_free (c->qmin_y);
      g_free (c->qmax_y);
      g_free (c);
    }
}

static Vec2 *
parse_grid_json (const gchar *json, gint expected_count)
{
  if (!json || !*json || expected_count <= 0) return NULL;
  Vec2 *out = g_new (Vec2, expected_count);
  const gchar *p = json;
  gint i = 0;

  while (*p && i < expected_count)
    {
      while (*p)
        {
          if (*p == '[')
            {
              const gchar *q = p + 1;
              while (*q && g_ascii_isspace (*q)) q++;
              if (g_ascii_isdigit (*q) || *q == '-' || *q == '.' || *q == '+')
                {
                  p = q;
                  break;
                }
            }
          p++;
        }
      if (!*p) break;

      gchar *endptr;
      gdouble x = g_ascii_strtod (p, &endptr);
      if (endptr == p) { g_free (out); return NULL; }
      p = endptr;
      while (*p && (*p == ',' || g_ascii_isspace (*p))) p++;
      gdouble y = g_ascii_strtod (p, &endptr);
      if (endptr == p) { g_free (out); return NULL; }
      p = endptr;
      while (*p && *p != ']') p++;
      if (!*p) { g_free (out); return NULL; }
      p++;
      out[i].x = x;
      out[i].y = y;
      i++;
    }

  if (i != expected_count)
    {
      g_free (out);
      return NULL;
    }

  return out;
}

static MeshCache *
build_mesh_cache (GeglProperties *o, uint64_t h, gsize len)
{
  gint cols = o->cols, rows = o->rows;
  if (cols < 1 || rows < 1) return NULL;

  gint n = (cols + 1) * (rows + 1);
  Vec2 *grid = parse_grid_json (o->grid_json, n);
  if (!grid) return NULL;

  MeshCache *c = g_new0 (MeshCache, 1);
  c->ref_count = 1;
  c->grid      = grid;
  c->n         = n;
  c->cols      = cols;
  c->rows      = rows;
  c->grid_json = g_strdup (o->grid_json);
  c->grid_hash = h;
  c->grid_len  = len;

  gint n_quads = cols * rows;
  c->qmin_x = g_new (gdouble, n_quads);
  c->qmax_x = g_new (gdouble, n_quads);
  c->qmin_y = g_new (gdouble, n_quads);
  c->qmax_y = g_new (gdouble, n_quads);

  for (gint r = 0; r < rows; r++)
    for (gint cc = 0; cc < cols; cc++)
      {
        Vec2 d00 = grid[r       * (cols + 1) + cc];
        Vec2 d10 = grid[r       * (cols + 1) + (cc + 1)];
        Vec2 d11 = grid[(r + 1) * (cols + 1) + (cc + 1)];
        Vec2 d01 = grid[(r + 1) * (cols + 1) + cc];
        gint qi  = r * cols + cc;

        c->qmin_x[qi] = MIN (MIN (d00.x, d10.x), MIN (d11.x, d01.x));
        c->qmax_x[qi] = MAX (MAX (d00.x, d10.x), MAX (d11.x, d01.x));
        c->qmin_y[qi] = MIN (MIN (d00.y, d10.y), MIN (d11.y, d01.y));
        c->qmax_y[qi] = MAX (MAX (d00.y, d10.y), MAX (d11.y, d01.y));
      }

  return c;
}

static inline gboolean
cache_is_valid (MeshCache *c, GeglProperties *o, uint64_t h, gsize len)
{
  return c &&
         c->cols == o->cols &&
         c->rows == o->rows &&
         c->grid_len == len &&
         c->grid_hash == h &&
         c->grid_json &&
         o->grid_json &&
         strcmp (c->grid_json, o->grid_json) == 0;
}

/* Returns a ref-counted MeshCache; caller must call mesh_cache_unref(cache). */
static MeshCache *
get_mesh_cache (GeglOperation *operation)
{
  GeglProperties *o = GEGL_PROPERTIES (operation);
  const gchar *gj = o->grid_json ? o->grid_json : "";
  gsize len = strlen (gj);
  uint64_t h = mw_fnv1a (gj, len);
  MeshCache *cache;

  g_mutex_lock (&g_cache_mutex);
  cache = g_object_get_data (G_OBJECT (operation), "mesh-warp-cache");
  if (!cache_is_valid (cache, o, h, len))
    {
      if (cache)
        g_object_set_data (G_OBJECT (operation), "mesh-warp-cache", NULL);
      cache = build_mesh_cache (o, h, len);
      if (cache)
        g_object_set_data_full (G_OBJECT (operation), "mesh-warp-cache",
                                cache, mesh_cache_unref);
    }
  if (cache)
    g_atomic_int_inc (&cache->ref_count);
  g_mutex_unlock (&g_cache_mutex);
  return cache;
}

static void
prepare (GeglOperation *operation)
{
  const Babl *format = babl_format ("R'G'B'A float");
  gegl_operation_set_format (operation, "input",  format);
  gegl_operation_set_format (operation, "output", format);
}

static GeglRectangle
get_bounding_box (GeglOperation *operation)
{
  const GeglRectangle *src_rect =
    gegl_operation_source_get_bounding_box (operation, "input");
  GeglRectangle in_rect = src_rect ? *src_rect : (GeglRectangle){0, 0, 0, 0};
  GeglRectangle result  = in_rect;

  MeshCache *cache = get_mesh_cache (operation);
  if (!cache) return result;

  gint n_quads = cache->cols * cache->rows;
  gdouble mnx = cache->qmin_x[0], mxx = cache->qmax_x[0];
  gdouble mny = cache->qmin_y[0], mxy = cache->qmax_y[0];
  for (gint i = 1; i < n_quads; i++)
    {
      if (cache->qmin_x[i] < mnx) mnx = cache->qmin_x[i];
      if (cache->qmax_x[i] > mxx) mxx = cache->qmax_x[i];
      if (cache->qmin_y[i] < mny) mny = cache->qmin_y[i];
      if (cache->qmax_y[i] > mxy) mxy = cache->qmax_y[i];
    }

  mesh_cache_unref (cache);

  gint new_x  = MIN (in_rect.x, (gint) floor (mnx));
  gint new_y  = MIN (in_rect.y, (gint) floor (mny));
  gint new_x2 = MAX (in_rect.x + in_rect.width,  (gint) ceil (mxx));
  gint new_y2 = MAX (in_rect.y + in_rect.height, (gint) ceil (mxy));
  result.x = new_x;
  result.y = new_y;
  result.width  = new_x2 - new_x;
  result.height = new_y2 - new_y;
  return result;
}

static gboolean
compute_required_source (const MeshCache      *cache,
                         const GeglProperties *o,
                         const GeglRectangle  *roi,
                         const GeglRectangle  *src_rect,
                         GeglRectangle        *out_rect)
{
  const gint cols = cache->cols;
  const gint rows = cache->rows;
  gint orig_w = o->orig_width  > 0 ? o->orig_width  : src_rect->width;
  gint orig_h = o->orig_height > 0 ? o->orig_height : src_rect->height;
  gdouble sx_scale = (gdouble) orig_w / cols;
  gdouble sy_scale = (gdouble) orig_h / rows;
  gint rq_x2 = roi->x + roi->width;
  gint rq_y2 = roi->y + roi->height;

  gint min_x = G_MAXINT, min_y = G_MAXINT;
  gint max_x = G_MININT, max_y = G_MININT;
  gboolean any = FALSE;

  for (gint r = 0; r < rows; r++)
    for (gint cc = 0; cc < cols; cc++)
      {
        gint qi = r * cols + cc;
        if (cache->qmax_x[qi] < roi->x) continue;
        if (cache->qmin_x[qi] >= rq_x2) continue;
        if (cache->qmax_y[qi] < roi->y) continue;
        if (cache->qmin_y[qi] >= rq_y2) continue;

        gint sx0 = o->pad_left + (gint) floor (cc * sx_scale);
        gint sx1 = o->pad_left + (gint) ceil ((cc + 1) * sx_scale);
        gint sy0 = o->pad_top  + (gint) floor (r  * sy_scale);
        gint sy1 = o->pad_top  + (gint) ceil ((r + 1) * sy_scale);

        if (sx0 < min_x) min_x = sx0;
        if (sy0 < min_y) min_y = sy0;
        if (sx1 > max_x) max_x = sx1;
        if (sy1 > max_y) max_y = sy1;
        any = TRUE;
      }

  if (!any)
    return FALSE;

  GeglRectangle want;
  want.x = min_x - 2;
  want.y = min_y - 2;
  want.width  = (max_x - min_x) + 5;
  want.height = (max_y - min_y) + 5;

  return gegl_rectangle_intersect (out_rect, &want, src_rect);
}

static GeglRectangle
get_required_for_output (GeglOperation       *operation,
                          const gchar         *input_pad,
                          const GeglRectangle *roi)
{
  GeglProperties *o = GEGL_PROPERTIES (operation);
  const GeglRectangle *src_rect =
    gegl_operation_source_get_bounding_box (operation, "input");

  (void) input_pad;
  if (!src_rect) return *roi;

  MeshCache *cache = get_mesh_cache (operation);
  if (!cache) return *src_rect;

  GeglRectangle need;
  gboolean ok = compute_required_source (cache, o, roi, src_rect, &need);
  mesh_cache_unref (cache);

  if (!ok)
    return (GeglRectangle){0, 0, 0, 0};

  return need;
}

static inline void
mw_quad_scanline_span (const double qx[4], const double qy[4], double fy,
                       double *out_lo, double *out_hi, gboolean *out_any)
{
  static const int segs[6][2] = {
    {0, 1}, {1, 2}, {2, 3}, {3, 0}, {0, 2}, {1, 3}
  };
  double lo = 1e300, hi = -1e300;
  gboolean any = FALSE;

  for (int s = 0; s < 6; s++)
    {
      int ia = segs[s][0], ib = segs[s][1];
      double ya = qy[ia], yb = qy[ib];
      double xa = qx[ia], xb = qx[ib];
      double ylo = ya < yb ? ya : yb;
      double yhi = ya > yb ? ya : yb;

      if (fy < ylo - 1e-6 || fy > yhi + 1e-6)
        continue;

      if (fabs (yb - ya) < 1e-12)
        {
          lo = fmin (lo, fmin (xa, xb));
          hi = fmax (hi, fmax (xa, xb));
        }
      else
        {
          double t = (fy - ya) / (yb - ya);
          if (t < 0.0) t = 0.0;
          if (t > 1.0) t = 1.0;
          double xi = xa + t * (xb - xa);
          lo = fmin (lo, xi);
          hi = fmax (hi, xi);
        }
      any = TRUE;
    }

  *out_lo = lo;
  *out_hi = hi;
  *out_any = any;
}

static inline void
mw_sample_premul_f (const float *buf, const GeglRectangle *need,
                    double sx, double sy, float out[4])
{
  double fx = sx - 0.5;
  double fy = sy - 0.5;
  int ix = (int) floor (fx);
  int iy = (int) floor (fy);
  double tx = fx - (double) ix;
  double ty = fy - (double) iy;
  const int rw = need->width, rh = need->height;
  int x0 = ix - need->x;
  int y0 = iy - need->y;
  int x1 = x0 + 1;
  int y1 = y0 + 1;

  if (x0 < 0) x0 = 0; else if (x0 >= rw) x0 = rw - 1;
  if (x1 < 0) x1 = 0; else if (x1 >= rw) x1 = rw - 1;
  if (y0 < 0) y0 = 0; else if (y0 >= rh) y0 = rh - 1;
  if (y1 < 0) y1 = 0; else if (y1 >= rh) y1 = rh - 1;

  const float *p00 = buf + ((size_t) y0 * rw + x0) * 4;
  const float *p10 = buf + ((size_t) y0 * rw + x1) * 4;
  const float *p01 = buf + ((size_t) y1 * rw + x0) * 4;
  const float *p11 = buf + ((size_t) y1 * rw + x1) * 4;

  double w00 = (1.0 - tx) * (1.0 - ty);
  double w10 = tx * (1.0 - ty);
  double w01 = (1.0 - tx) * ty;
  double w11 = tx * ty;

  double r = w00 * p00[0] + w10 * p10[0] + w01 * p01[0] + w11 * p11[0];
  double g = w00 * p00[1] + w10 * p10[1] + w01 * p01[1] + w11 * p11[1];
  double b = w00 * p00[2] + w10 * p10[2] + w01 * p01[2] + w11 * p11[2];
  double a = w00 * p00[3] + w10 * p10[3] + w01 * p01[3] + w11 * p11[3];

  if (a > 1e-6)
    {
      double inva = 1.0 / a;
      out[0] = (float) (r * inva);
      out[1] = (float) (g * inva);
      out[2] = (float) (b * inva);
      out[3] = (float) (a > 1.0 ? 1.0 : a);
    }
  else
    {
      out[0] = out[1] = out[2] = out[3] = 0.0f;
    }
}

/* NOTE: no OpenMP here. GEGL already parallelizes process() at the tile
 * level; adding an inner omp parallel for would oversubscribe cores. */
static gboolean
process (GeglOperation       *operation,
         GeglBuffer          *input,
         GeglBuffer          *output,
         const GeglRectangle *result,
         gint                 level)
{
  GeglProperties *o = GEGL_PROPERTIES (operation);
  const GeglRectangle *src_rect =
    gegl_operation_source_get_bounding_box (operation, "input");
  GeglRectangle in_extent = src_rect ? *src_rect : *result;

  (void) level;

  MeshCache *cache = get_mesh_cache (operation);
  if (!cache)
    {
      gegl_buffer_copy (input, result, GEGL_ABYSS_NONE, output, result);
      return TRUE;
    }

  const gsize stride = (gsize) result->width * 4;
  gfloat *out_buf = g_new0 (gfloat, stride * result->height);

  GeglRectangle need;
  if (!compute_required_source (cache, o, result, &in_extent, &need) ||
      need.width <= 0 || need.height <= 0)
    {
      gegl_buffer_set (output, result, 0, babl_format ("RGBA float"), out_buf,
                       GEGL_AUTO_ROWSTRIDE);
      g_free (out_buf);
      mesh_cache_unref (cache);
      return TRUE;
    }

  gfloat *in_buf = g_new (gfloat, (gsize) need.width * (gsize) need.height * 4);
  gegl_buffer_get (input, &need, 1.0, babl_format ("RaGaBaA float"), in_buf,
                   GEGL_AUTO_ROWSTRIDE, GEGL_ABYSS_NONE);

  const gint cols = cache->cols, rows = cache->rows;
  const gint rq_x2 = result->x + result->width;
  const gint rq_y2 = result->y + result->height;
  const gint orig_w = o->orig_width  > 0 ? o->orig_width  : in_extent.width;
  const gint orig_h = o->orig_height > 0 ? o->orig_height : in_extent.height;
  const gdouble src_x_scale = (gdouble) orig_w / cols;
  const gdouble src_y_scale = (gdouble) orig_h / rows;

  const gdouble cx_lo = MAX ((gdouble) in_extent.x + 0.5, (gdouble) o->pad_left + 0.5);
  const gdouble cx_hi = MIN ((gdouble) (in_extent.x + in_extent.width) - 0.5,
                             (gdouble) o->pad_left + (gdouble) orig_w - 0.5);
  const gdouble cy_lo = MAX ((gdouble) in_extent.y + 0.5, (gdouble) o->pad_top + 0.5);
  const gdouble cy_hi = MIN ((gdouble) (in_extent.y + in_extent.height) - 0.5,
                             (gdouble) o->pad_top + (gdouble) orig_h - 0.5);

  for (gint r = 0; r < rows; r++)
    for (gint cc = 0; cc < cols; cc++)
      {
        gint qi = r * cols + cc;
        gint min_x = MAX (result->x, (gint) floor (cache->qmin_x[qi]));
        gint max_x = MIN (rq_x2 - 1, (gint) ceil  (cache->qmax_x[qi]));
        gint min_y = MAX (result->y, (gint) floor (cache->qmin_y[qi]));
        gint max_y = MIN (rq_y2 - 1, (gint) ceil  (cache->qmax_y[qi]));
        if (min_x > max_x || min_y > max_y) continue;

        Vec2 d00 = cache->grid[r       * (cols + 1) + cc];
        Vec2 d10 = cache->grid[r       * (cols + 1) + (cc + 1)];
        Vec2 d11 = cache->grid[(r + 1) * (cols + 1) + (cc + 1)];
        Vec2 d01 = cache->grid[(r + 1) * (cols + 1) + cc];

        const double qx[4] = { d00.x, d10.x, d11.x, d01.x };
        const double qy[4] = { d00.y, d10.y, d11.y, d01.y };

        const gdouble a1 = d00.x;
        const gdouble b1 = d10.x - d00.x;
        const gdouble c1 = d01.x - d00.x;
        const gdouble d1 = d00.x - d10.x + d11.x - d01.x;
        const gdouble a2 = d00.y;
        const gdouble b2 = d10.y - d00.y;
        const gdouble c2 = d01.y - d00.y;
        const gdouble d2 = d00.y - d10.y + d11.y - d01.y;

        const gdouble A     = d2 * c1 - c2 * d1;
        const gboolean az   = fabs (A) < 1e-9;
        const gdouble fourA = 4.0 * A;
        const gdouble inv2A = az ? 0.0 : (0.5 / A);
        const gdouble kBC   = -c2 * b1 + b2 * c1;

        const gdouble src_x_base = o->pad_left + cc * src_x_scale;
        const gdouble src_y_base = o->pad_top  + r  * src_y_scale;

        for (gint y = min_y; y <= max_y; y++)
          {
            const gdouble fy = (gdouble) y + 0.5;
            double lo, hi;
            gboolean any;

            mw_quad_scanline_span (qx, qy, fy, &lo, &hi, &any);
            if (!any) continue;

            gint xstart = MAX (min_x, (gint) floor (lo - 1.0));
            gint xend   = MIN (max_x, (gint) ceil  (hi + 1.0));
            if (xstart > xend) continue;

            const gdouble py = fy - a2;
            const gdouble By = py * d1 + kBC;
            const gdouble Cy = py * b1;
            const gsize row_off = (gsize)(y - result->y) * stride;

            gdouble px = ((gdouble) xstart + 0.5) - a1;
            gdouble B  = By - d2 * px;
            gdouble C  = Cy - b2 * px;

            for (gint x = xstart; x <= xend; x++, px += 1.0, B -= d2, C -= b2)
              {
                gdouble v;
                if (az)
                  {
                    v = (fabs (B) < 1e-9) ? 0.5 : -C / B;
                  }
                else
                  {
                    gdouble disc = B * B - fourA * C;
                    if (disc < 0.0) continue;
                    gdouble sq = sqrt (disc);
                    gdouble v1 = (-B + sq) * inv2A;
                    if (v1 >= -0.005 && v1 <= 1.005)
                      v = v1;
                    else
                      {
                        gdouble v2 = (-B - sq) * inv2A;
                        if (v2 >= -0.005 && v2 <= 1.005) v = v2;
                        else continue;
                      }
                  }

                gdouble denom = b1 + d1 * v;
                gdouble u;
                if (fabs (denom) > 1e-7)
                  u = (px - c1 * v) / denom;
                else
                  {
                    gdouble dy_dn = b2 + d2 * v;
                    if (fabs (dy_dn) <= 1e-7) continue;
                    u = (py - c2 * v) / dy_dn;
                  }

                if (u < -0.002 || u > 1.002 || v < -0.002 || v > 1.002)
                  continue;

                u = CLAMP (u, 0.0, 1.0);
                v = CLAMP (v, 0.0, 1.0);

                gdouble sx = src_x_base + u * src_x_scale;
                gdouble sy = src_y_base + v * src_y_scale;
                if (cx_hi > cx_lo) sx = CLAMP (sx, cx_lo, cx_hi);
                if (cy_hi > cy_lo) sy = CLAMP (sy, cy_lo, cy_hi);

                gfloat pixel[4];
                mw_sample_premul_f (in_buf, &need, sx, sy, pixel);
                if (!(pixel[3] > 0.0f)) continue;

                const gsize di = row_off + (gsize)(x - result->x) * 4;
                if (pixel[3] < out_buf[di + 3]) continue;

                out_buf[di + 0] = pixel[0];
                out_buf[di + 1] = pixel[1];
                out_buf[di + 2] = pixel[2];
                out_buf[di + 3] = pixel[3];
              }
          }
      }

  gegl_buffer_set (output, result, 0, babl_format ("RGBA float"), out_buf,
                   GEGL_AUTO_ROWSTRIDE);
  g_free (in_buf);
  g_free (out_buf);
  mesh_cache_unref (cache);
  return TRUE;
}

static void
gegl_op_class_init (GeglOpClass *klass)
{
  GeglOperationClass       *operation_class = GEGL_OPERATION_CLASS (klass);
  GeglOperationFilterClass *filter_class    = GEGL_OPERATION_FILTER_CLASS (klass);

  operation_class->prepare                 = prepare;
  operation_class->get_bounding_box        = get_bounding_box;
  operation_class->get_required_for_output = get_required_for_output;
  filter_class->process                    = process;

  gegl_operation_class_set_keys (operation_class,
    "name",        "custom:mesh-warp",
    "title",       "Mesh Warp",
    "categories",  "distort",
    "description", "Arbitrary mesh-grid backward warp with puppet-pin passthrough.",
    NULL);
}

/* ==================================================================
 * ctypes-callable utility functions (compiled once; exported).
 * ================================================================== */

#define MW_MIN(a,b)      ((a) < (b) ? (a) : (b))
#define MW_MAX(a,b)      ((a) > (b) ? (a) : (b))
#define MW_CLAMP(x,l,h)  ((x) < (l) ? (l) : ((x) > (h) ? (h) : (x)))

typedef struct
{
  double qx[4], qy[4];
  double a1w, a2w, b1, c1, d1, b2, c2, d2;
  double A, fourA, inv2A, kBC;
  int    a_0;
  double s00x, s00y, s10x, s10y, s01x, s01y, s11x, s11y;
  int    bx0, bx1, by0, by1;
} MwPrepQuad;

static inline uint32_t
mw_sample_argb32_fast (const uint32_t *src, int src_w, int src_h,
                       double fx, double fy)
{
  int ix0 = (int) floor (fx);
  int iy0 = (int) floor (fy);
  int tx  = (int) ((fx - (double) ix0) * 256.0 + 0.5);
  int ty  = (int) ((fy - (double) iy0) * 256.0 + 0.5);

  if (tx < 0) tx = 0; else if (tx > 256) tx = 256;
  if (ty < 0) ty = 0; else if (ty > 256) ty = 256;

  int cx0 = MW_CLAMP (ix0,     0, src_w - 1);
  int cx1 = MW_CLAMP (ix0 + 1, 0, src_w - 1);
  int cy0 = MW_CLAMP (iy0,     0, src_h - 1);
  int cy1 = MW_CLAMP (iy0 + 1, 0, src_h - 1);

  uint32_t p00 = src[(size_t) cy0 * (size_t) src_w + (size_t) cx0];
  uint32_t p10 = src[(size_t) cy0 * (size_t) src_w + (size_t) cx1];
  uint32_t p01 = src[(size_t) cy1 * (size_t) src_w + (size_t) cx0];
  uint32_t p11 = src[(size_t) cy1 * (size_t) src_w + (size_t) cx1];

  uint32_t w00 = (uint32_t) (((256 - tx) * (256 - ty)) >> 8);
  uint32_t w10 = (uint32_t) ((tx * (256 - ty)) >> 8);
  uint32_t w01 = (uint32_t) (((256 - tx) * ty) >> 8);
  int      w11_i = 256 - (int) w00 - (int) w10 - (int) w01;
  uint32_t w11 = (uint32_t) (w11_i > 0 ? w11_i : 0);

  uint32_t a = ((p00 >> 24) * w00 + (p10 >> 24) * w10 +
                (p01 >> 24) * w01 + (p11 >> 24) * w11) >> 8;
  uint32_t r = (((p00 >> 16) & 255u) * w00 + ((p10 >> 16) & 255u) * w10 +
                ((p01 >> 16) & 255u) * w01 + ((p11 >> 16) & 255u) * w11) >> 8;
  uint32_t g = (((p00 >>  8) & 255u) * w00 + ((p10 >>  8) & 255u) * w10 +
                ((p01 >>  8) & 255u) * w01 + ((p11 >>  8) & 255u) * w11) >> 8;
  uint32_t b = ((p00 & 255u) * w00 + (p10 & 255u) * w10 +
                (p01 & 255u) * w01 + (p11 & 255u) * w11) >> 8;

  if (a > 255u) a = 255u;
  if (r > 255u) r = 255u;
  if (g > 255u) g = 255u;
  if (b > 255u) b = 255u;

  return (a << 24) | (r << 16) | (g << 8) | b;
}

static inline void
mw_blend_over (uint32_t *dp, uint32_t src)
{
  uint32_t sa = src >> 24;
  if (sa == 0u) return;

  uint32_t dst = *dp;
  if (sa == 255u || dst == 0u)
    {
      *dp = src;
      return;
    }

  uint32_t inv = 255u - sa;
  uint32_t oa  = sa + (((dst >> 24) * inv + 127u) / 255u);
  uint32_t cr  = ((src >> 16) & 255u) + ((((dst >> 16) & 255u) * inv + 127u) / 255u);
  uint32_t cg  = ((src >>  8) & 255u) + ((((dst >>  8) & 255u) * inv + 127u) / 255u);
  uint32_t cb  = (src & 255u) + (((dst & 255u) * inv + 127u) / 255u);

  if (oa > 255u) oa = 255u;
  if (cr > 255u) cr = 255u;
  if (cg > 255u) cg = 255u;
  if (cb > 255u) cb = 255u;

  *dp = (oa << 24) | (cr << 16) | (cg << 8) | cb;
}

static void
mw_render_impl (uint32_t       *dst, int dst_w, int dst_h,
                const uint32_t *src, int src_w, int src_h,
                const double   *dst_grid,
                const double   *src_grid,
                int cols, int rows,
                double pad_left, double pad_top,
                double src_scale_x, double src_scale_y,
                double zoom, double pan_x, double pan_y,
                int draw_bg, int canvas_w, int canvas_h, int show_checker)
{
  if (!dst || !src || !dst_grid || !src_grid ||
      dst_w <= 0 || dst_h <= 0 || src_w <= 0 || src_h <= 0 ||
      cols < 1 || rows < 1 || !(zoom > 0.0))
    return;

  const double inv_zoom  = 1.0 / zoom;
  const double src_off_x = pad_left * src_scale_x;
  const double src_off_y = pad_top  * src_scale_y;
  const int    n_quads   = cols * rows;

  MwPrepQuad *quads = (MwPrepQuad *) malloc ((size_t) n_quads * sizeof (MwPrepQuad));
  if (!quads) return;

  int n_vis = 0;
  for (int qi = 0; qi < n_quads; qi++)
    {
      const int r  = qi / cols;
      const int cc = qi % cols;
      const int v00 =  r      * (cols + 1) + cc;
      const int v10 =  r      * (cols + 1) + cc + 1;
      const int v01 = (r + 1) * (cols + 1) + cc;
      const int v11 = (r + 1) * (cols + 1) + cc + 1;

      const double a1w  = pad_left + dst_grid[v00 * 2];
      const double a2w  = pad_top  + dst_grid[v00 * 2 + 1];
      const double dx10 = pad_left + dst_grid[v10 * 2];
      const double dy10 = pad_top  + dst_grid[v10 * 2 + 1];
      const double dx01 = pad_left + dst_grid[v01 * 2];
      const double dy01 = pad_top  + dst_grid[v01 * 2 + 1];
      const double dx11 = pad_left + dst_grid[v11 * 2];
      const double dy11 = pad_top  + dst_grid[v11 * 2 + 1];

      const double wminx = MW_MIN (MW_MIN (a1w, dx10), MW_MIN (dx01, dx11));
      const double wmaxx = MW_MAX (MW_MAX (a1w, dx10), MW_MAX (dx01, dx11));
      const double wminy = MW_MIN (MW_MIN (a2w, dy10), MW_MIN (dy01, dy11));
      const double wmaxy = MW_MAX (MW_MAX (a2w, dy10), MW_MAX (dy01, dy11));

      int bx0 = MW_MAX (0,         (int) floor (pan_x + zoom * wminx - 1.0));
      int bx1 = MW_MIN (dst_w - 1, (int) ceil  (pan_x + zoom * wmaxx + 1.0));
      int by0 = MW_MAX (0,         (int) floor (pan_y + zoom * wminy - 1.0));
      int by1 = MW_MIN (dst_h - 1, (int) ceil  (pan_y + zoom * wmaxy + 1.0));
      if (bx0 > bx1 || by0 > by1) continue;

      MwPrepQuad *q = &quads[n_vis++];
      q->qx[0] = pan_x + zoom * a1w;   q->qy[0] = pan_y + zoom * a2w;
      q->qx[1] = pan_x + zoom * dx10;  q->qy[1] = pan_y + zoom * dy10;
      q->qx[2] = pan_x + zoom * dx11;  q->qy[2] = pan_y + zoom * dy11;
      q->qx[3] = pan_x + zoom * dx01;  q->qy[3] = pan_y + zoom * dy01;

      q->a1w = a1w;
      q->a2w = a2w;
      q->b1  = dx10 - a1w;
      q->c1  = dx01 - a1w;
      q->d1  = a1w - dx10 + dx11 - dx01;
      q->b2  = dy10 - a2w;
      q->c2  = dy01 - a2w;
      q->d2  = a2w - dy10 + dy11 - dy01;

      q->A     = q->d2 * q->c1 - q->c2 * q->d1;
      q->a_0   = fabs (q->A) < 1e-9;
      q->fourA = 4.0 * q->A;
      q->inv2A = q->a_0 ? 0.0 : (0.5 / q->A);
      q->kBC   = -q->c2 * q->b1 + q->b2 * q->c1;

      q->s00x = src_grid[v00 * 2]; q->s00y = src_grid[v00 * 2 + 1];
      q->s10x = src_grid[v10 * 2]; q->s10y = src_grid[v10 * 2 + 1];
      q->s01x = src_grid[v01 * 2]; q->s01y = src_grid[v01 * 2 + 1];
      q->s11x = src_grid[v11 * 2]; q->s11y = src_grid[v11 * 2 + 1];

      q->bx0 = bx0; q->bx1 = bx1;
      q->by0 = by0; q->by1 = by1;
    }

  uint32_t *ck_rows = NULL;
  int cx0 = 0, cx1 = 0, cy0 = 0, cy1 = 0, ck_w = 0;
  double ck_step = 16.0;
  const uint32_t bg_win = 0xFF212124u;
  const uint32_t ck_a   = 0xFFD9D9D9u;
  const uint32_t ck_b   = 0xFFA6A6A6u;

  if (draw_bg && show_checker)
    {
      cx0 = (int) floor (pan_x);
      cx1 = (int) ceil  ((double) canvas_w * zoom + pan_x);
      cy0 = (int) floor (pan_y);
      cy1 = (int) ceil  ((double) canvas_h * zoom + pan_y);
      if (cx0 < 0) cx0 = 0;
      if (cx1 > dst_w) cx1 = dst_w;
      if (cy0 < 0) cy0 = 0;
      if (cy1 > dst_h) cy1 = dst_h;

      while (ck_step * zoom < 5.0)
        ck_step *= 2.0;

      ck_w = cx1 - cx0;
      if (ck_w > 0 && cy0 < cy1)
        {
          ck_rows = (uint32_t *) malloc ((size_t) ck_w * 2u * sizeof (uint32_t));
          if (ck_rows)
            {
              double du = 1.0 / (zoom * ck_step);
              double u  = (((double) cx0 + 0.5 - pan_x) / zoom) / ck_step;
              for (int xx = 0; xx < ck_w; xx++, u += du)
                {
                  int cck = (int) floor (u);
                  ck_rows[xx]        = (cck & 1) ? ck_b : ck_a;
                  ck_rows[ck_w + xx] = (cck & 1) ? ck_a : ck_b;
                }
            }
        }
    }

  int nbands = 1;
#ifdef _OPENMP
  nbands = omp_get_max_threads ();
  if (nbands < 1) nbands = 1;
  if (nbands > 16) nbands = 16;
  if (nbands * 16 > dst_h)
    nbands = (dst_h + 15) / 16;
  if (nbands < 1) nbands = 1;
#endif
  int band_h = (dst_h + nbands - 1) / nbands;
  if (band_h < 1) band_h = 1;

#ifdef _OPENMP
#pragma omp parallel for schedule(static)
#endif
  for (int b = 0; b < nbands; b++)
    {
      int y0 = b * band_h;
      int y1 = y0 + band_h;
      if (y1 > dst_h) y1 = dst_h;
      if (y0 >= y1) continue;

      const int bh = y1 - y0;
      uint32_t *wbuf = NULL;
      int own_wbuf = 0;

      if (draw_bg)
        {
          double ck_inv_zstep = 1.0 / (zoom * ck_step);
          for (int yy = y0; yy < y1; yy++)
            {
              uint32_t *row32 = dst + (size_t) yy * (size_t) dst_w;
              if (ck_rows && yy >= cy0 && yy < cy1)
                {
                  int rck = (int) floor (((double) yy + 0.5 - pan_y) * ck_inv_zstep);
                  for (int xx = 0; xx < cx0; xx++)
                    row32[xx] = bg_win;
                  memcpy (row32 + cx0,
                          ck_rows + (size_t) (rck & 1) * (size_t) ck_w,
                          (size_t) ck_w * sizeof (uint32_t));
                  for (int xx = cx1; xx < dst_w; xx++)
                    row32[xx] = bg_win;
                }
              else
                {
                  for (int xx = 0; xx < dst_w; xx++)
                    row32[xx] = bg_win;
                }
            }
          wbuf = (uint32_t *) calloc ((size_t) bh * (size_t) dst_w, sizeof (uint32_t));
          if (wbuf)
            own_wbuf = 1;
        }
      else
        {
          wbuf = dst + (size_t) y0 * (size_t) dst_w;
          memset (wbuf, 0, (size_t) bh * (size_t) dst_w * sizeof (uint32_t));
        }

      for (int qi = 0; qi < n_vis; qi++)
        {
          const MwPrepQuad *q = &quads[qi];
          int sy0 = MW_MAX (q->by0, y0);
          int sy1 = MW_MIN (q->by1, y1 - 1);
          if (sy0 > sy1) continue;

          const double d2_step = q->d2 * inv_zoom;
          const double b2_step = q->b2 * inv_zoom;

          for (int sy = sy0; sy <= sy1; sy++)
            {
              const double fsy = (double) sy + 0.5;
              double lo, hi;
              gboolean any;

              mw_quad_scanline_span (q->qx, q->qy, fsy, &lo, &hi, &any);
              if (!any) continue;

              int xstart = MW_MAX (q->bx0, (int) floor (lo - 1.0));
              int xend   = MW_MIN (q->bx1, (int) ceil  (hi + 1.0));
              if (xstart > xend) continue;

              const double wy = (fsy - pan_y) * inv_zoom;
              const double py = wy - q->a2w;
              const double By = py * q->d1 + q->kBC;
              const double Cy = py * q->b1;

              double wx = ((double) xstart + 0.5 - pan_x) * inv_zoom;
              double px = wx - q->a1w;
              double B  = By - q->d2 * px;
              double C  = Cy - q->b2 * px;
              uint32_t *wrow = wbuf ? (wbuf + (size_t) (sy - y0) * (size_t) dst_w)
                                    : (dst  + (size_t) sy * (size_t) dst_w);

              for (int sx = xstart; sx <= xend; sx++, px += inv_zoom, B -= d2_step, C -= b2_step)
                {
                  double v;
                  if (q->a_0)
                    {
                      v = (fabs (B) < 1e-9) ? 0.5 : -C / B;
                    }
                  else
                    {
                      double disc = B * B - q->fourA * C;
                      if (disc < 0.0) continue;
                      double sq = sqrt (disc);
                      double v1 = (-B + sq) * q->inv2A;
                      if (v1 >= -0.002 && v1 <= 1.002)
                        v = v1;
                      else
                        {
                          double v2 = (-B - sq) * q->inv2A;
                          if (v2 >= -0.002 && v2 <= 1.002) v = v2;
                          else continue;
                        }
                    }

                  double denom = q->b1 + q->d1 * v;
                  double u;
                  if (fabs (denom) > 1e-7)
                    u = (px - q->c1 * v) / denom;
                  else
                    {
                      double dy_dn = q->b2 + q->d2 * v;
                      if (fabs (dy_dn) <= 1e-7) continue;
                      u = (py - q->c2 * v) / dy_dn;
                    }

                  if (u < -0.002 || u > 1.002 || v < -0.002 || v > 1.002)
                    continue;

                  u = MW_CLAMP (u, 0.0, 1.0);
                  v = MW_CLAMP (v, 0.0, 1.0);

                  const double su = 1.0 - u, sv = 1.0 - v;
                  const double w00 = su * sv, w10 = u * sv, w01 = su * v, w11 = u * v;
                  const double ix_w = w00 * q->s00x + w10 * q->s10x +
                                      w01 * q->s01x + w11 * q->s11x;
                  const double iy_w = w00 * q->s00y + w10 * q->s10y +
                                      w01 * q->s01y + w11 * q->s11y;

                  const double fx = ix_w * src_scale_x + src_off_x - 0.5;
                  const double fy = iy_w * src_scale_y + src_off_y - 0.5;

                  uint32_t col = mw_sample_argb32_fast (src, src_w, src_h, fx, fy);
                  uint32_t sa = col >> 24;
                  if (sa)
                    {
                      if (wbuf)
                        {
                          if (sa >= (wrow[sx] >> 24))
                            wrow[sx] = col;
                        }
                      else
                        {
                          mw_blend_over (&wrow[sx], col);
                        }
                    }
                }
            }
        }

      if (own_wbuf)
        {
          for (int yy = y0; yy < y1; yy++)
            {
              uint32_t *drow = dst + (size_t) yy * (size_t) dst_w;
              const uint32_t *wrow = wbuf + (size_t) (yy - y0) * (size_t) dst_w;
              for (int xx = 0; xx < dst_w; xx++)
                {
                  uint32_t col = wrow[xx];
                  if (col >> 24)
                    mw_blend_over (&drow[xx], col);
                }
            }
          free (wbuf);
        }
    }

  free (ck_rows);
  free (quads);
}

/* Backward warp into ARGB32 buffer sized to the on-screen widget. */
MW_EXPORT void
mesh_preview_render (uint32_t       *dst, int dst_w, int dst_h,
                     const uint32_t *src, int src_w, int src_h,
                     const double   *dst_grid,
                     const double   *src_grid,
                     int cols, int rows,
                     double pad_left, double pad_top,
                     double src_scale_x, double src_scale_y,
                     double zoom, double pan_x, double pan_y)
{
  mw_render_impl (dst, dst_w, dst_h, src, src_w, src_h,
                  dst_grid, src_grid, cols, rows,
                  pad_left, pad_top, src_scale_x, src_scale_y,
                  zoom, pan_x, pan_y,
                  0, 0, 0, 0);
}

/* 1-pass viewport compositor: window background + checkerboard + warped layer. */
MW_EXPORT void
mesh_preview_render_vp (uint32_t       *dst, int dst_w, int dst_h,
                        const uint32_t *src, int src_w, int src_h,
                        const double   *dst_grid,
                        const double   *src_grid,
                        int cols, int rows,
                        double pad_left, double pad_top,
                        double src_scale_x, double src_scale_y,
                        double zoom, double pan_x, double pan_y,
                        int canvas_w, int canvas_h, int show_checker)
{
  mw_render_impl (dst, dst_w, dst_h, src, src_w, src_h,
                  dst_grid, src_grid, cols, rows,
                  pad_left, pad_top, src_scale_x, src_scale_y,
                  zoom, pan_x, pan_y,
                  1, canvas_w, canvas_h, show_checker);
}

/* Windowed Gaussian RBF: orig_grid + pins -> out_grid. */
MW_EXPORT void
mesh_rbf_build (const double *orig_grid, int n_verts,
                const double *pins, int n_pins,
                double sigma, double *out_grid)
{
  if (n_pins <= 0)
    {
      memcpy (out_grid, orig_grid, (size_t) n_verts * 2 * sizeof (double));
      return;
    }
  const double R         = sigma * 2.5;
  const double R2        = R * R;
  const double inv_2sig2 = 1.0 / (2.0 * sigma * sigma);

#ifdef _OPENMP
#pragma omp parallel for schedule(static) if (n_verts * n_pins >= 8192)
#endif
  for (int i = 0; i < n_verts; i++)
    {
      const double ox = orig_grid[i * 2];
      const double oy = orig_grid[i * 2 + 1];
      double sw = 0.0, sdx = 0.0, sdy = 0.0;
      for (int j = 0; j < n_pins; j++)
        {
          const double px  = pins[j * 4];
          const double py  = pins[j * 4 + 1];
          const double ddx = pins[j * 4 + 2] - px;
          const double ddy = pins[j * 4 + 3] - py;
          const double dx  = ox - px;
          const double dy  = oy - py;
          const double d2  = dx * dx + dy * dy;
          if (d2 >= R2) continue;
          const double d   = sqrt (d2);
          const double g   = exp (-d2 * inv_2sig2);
          const double t   = d / R;
          const double win = 1.0 - t * t;
          const double w   = g * win * win;
          sw  += w;
          sdx += w * ddx;
          sdy += w * ddy;
        }
      if (sw > 1e-9)
        {
          const double denom = sw > 1.0 ? sw : 1.0;
          out_grid[i * 2]     = ox + sdx / denom;
          out_grid[i * 2 + 1] = oy + sdy / denom;
        }
      else
        {
          out_grid[i * 2]     = ox;
          out_grid[i * 2 + 1] = oy;
        }
    }
}

/* Grid resample to new topology. */
MW_EXPORT void
mesh_resample_grid (const double *old_grid, int old_cols, int old_rows,
                    double *new_grid, int new_cols, int new_rows)
{
#ifdef _OPENMP
#pragma omp parallel for collapse(2) schedule(static) if ((new_rows + 1) * (new_cols + 1) >= 4096)
#endif
  for (int r = 0; r <= new_rows; r++)
    for (int c = 0; c <= new_cols; c++)
      {
        const double v  = (double) r / new_rows;
        const double u  = (double) c / new_cols;
        const double gx = u * old_cols;
        const double gy = v * old_rows;
        const int    i0 = MW_CLAMP ((int) floor (gx), 0, old_cols - 1);
        const int    j0 = MW_CLAMP ((int) floor (gy), 0, old_rows - 1);
        const double tx = MW_CLAMP (gx - i0, 0.0, 1.0);
        const double ty = MW_CLAMP (gy - j0, 0.0, 1.0);
        const int p00 = j0       * (old_cols + 1) + i0;
        const int p10 = j0       * (old_cols + 1) + i0 + 1;
        const int p01 = (j0 + 1) * (old_cols + 1) + i0;
        const int p11 = (j0 + 1) * (old_cols + 1) + i0 + 1;
        const double x = (1-tx)*(1-ty)*old_grid[p00*2] + tx*(1-ty)*old_grid[p10*2] +
                         (1-tx)*ty*old_grid[p01*2]   + tx*ty*old_grid[p11*2];
        const double y = (1-tx)*(1-ty)*old_grid[p00*2+1] + tx*(1-ty)*old_grid[p10*2+1] +
                         (1-tx)*ty*old_grid[p01*2+1]   + tx*ty*old_grid[p11*2+1];
        const int out = r * (new_cols + 1) + c;
        new_grid[out * 2]     = x;
        new_grid[out * 2 + 1] = y;
      }
}

/* Laplacian smoothing. */
MW_EXPORT void
mesh_smooth_grid (double *grid, int cols, int rows,
                  double factor, int lock_boundary)
{
  const size_t bytes = (size_t)(cols + 1) * (rows + 1) * 2 * sizeof (double);
  double *src = malloc (bytes);
  if (!src) return;
  memcpy (src, grid, bytes);

#ifdef _OPENMP
#pragma omp parallel for collapse(2) schedule(static) if ((rows + 1) * (cols + 1) >= 4096)
#endif
  for (int r = 0; r <= rows; r++)
    for (int c = 0; c <= cols; c++)
      {
        const int boundary = (r == 0 || r == rows || c == 0 || c == cols);
        if (boundary && lock_boundary) continue;

        double ax = 0.0, ay = 0.0;
        int    n  = 0;
        if (c > 0)    { int k = r*(cols+1)+c-1; ax += src[k*2]; ay += src[k*2+1]; n++; }
        if (c < cols) { int k = r*(cols+1)+c+1; ax += src[k*2]; ay += src[k*2+1]; n++; }
        if (r > 0)    { int k = (r-1)*(cols+1)+c; ax += src[k*2]; ay += src[k*2+1]; n++; }
        if (r < rows) { int k = (r+1)*(cols+1)+c; ax += src[k*2]; ay += src[k*2+1]; n++; }
        if (n == 0) continue;
        ax /= n; ay /= n;
        const int k = r * (cols + 1) + c;
        grid[k*2]     = (1.0 - factor) * src[k*2]     + factor * ax;
        grid[k*2 + 1] = (1.0 - factor) * src[k*2 + 1] + factor * ay;
      }
  free (src);
}

/* Diagnostic: return number of OpenMP threads available at runtime. */
MW_EXPORT int
mesh_omp_max_threads (void)
{
#ifdef _OPENMP
  return omp_get_max_threads ();
#else
  return 0;
#endif
}

#endif
