/*
 * mesh-warp.c - Optimized GEGL operation: arbitrary mesh-grid backward warp.
 *
 * v2 additions:
 *   - GMutex around cache rebuild, prevents double-free / leak race when
 *     two GEGL worker threads notice a property hash change simultaneously.
 *   - pins_json + mode passthrough properties: Python stores puppet-pin
 *     state here for round-trip; the C op ignores both.
 *
 * BUILD:
 *   gcc -O2 -shared -fPIC mesh-warp.c -o mesh-warp.so \
 *       $(pkg-config --cflags --libs gegl-0.4 glib-2.0)
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

typedef struct { gdouble x, y; } Vec2;

typedef struct {
  Vec2    *grid;
  gint     n;
  gint     cols, rows;
  gdouble *qmin_x, *qmax_x, *qmin_y, *qmax_y;
  guint    hash;
} MeshCache;

/* Serializes cache rebuild across GEGL's worker threads. Static init to zero
 * is valid for GMutex on GLib 2.32+. */
static GMutex g_cache_mutex;

static void
mesh_cache_free (gpointer data)
{
  MeshCache *c = data;
  if (!c) return;
  g_free (c->grid);
  g_free (c->qmin_x); g_free (c->qmax_x);
  g_free (c->qmin_y); g_free (c->qmax_y);
  g_free (c);
}

static guint
compute_cache_hash (GeglProperties *o)
{
  guint h = o->grid_json ? g_str_hash (o->grid_json) : 0u;
  h = h * 31u + (guint) o->cols;
  h = h * 31u + (guint) o->rows;
  return h;
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
      while (*p && !(*p == '[' &&
                     (g_ascii_isdigit (p[1]) || p[1] == '-' || p[1] == '.')))
        p++;
      if (!*p) break;
      p++;

      gchar *endptr;
      gdouble x = g_ascii_strtod (p, &endptr);
      if (endptr == p) { g_free (out); return NULL; }
      p = endptr;

      while (*p && (*p == ',' || *p == ' ')) p++;

      gdouble y = g_ascii_strtod (p, &endptr);
      if (endptr == p) { g_free (out); return NULL; }
      p = endptr;

      while (*p && *p != ']') p++;
      if (!*p) { g_free (out); return NULL; }
      p++;

      out[i].x = x; out[i].y = y;
      i++;
    }

  if (i != expected_count) { g_free (out); return NULL; }
  return out;
}

static MeshCache *
build_mesh_cache (GeglProperties *o)
{
  gint cols = o->cols, rows = o->rows;
  if (cols < 1 || rows < 1) return NULL;

  gint n = (cols + 1) * (rows + 1);
  Vec2 *grid = parse_grid_json (o->grid_json, n);
  if (!grid) return NULL;

  MeshCache *c = g_new0 (MeshCache, 1);
  c->grid = grid;
  c->n = n;
  c->cols = cols;
  c->rows = rows;

  gint n_quads = cols * rows;
  c->qmin_x = g_new (gdouble, n_quads);
  c->qmax_x = g_new (gdouble, n_quads);
  c->qmin_y = g_new (gdouble, n_quads);
  c->qmax_y = g_new (gdouble, n_quads);

  for (gint r = 0; r < rows; r++)
    for (gint cc = 0; cc < cols; cc++)
      {
        Vec2 d00 = grid[r * (cols + 1) + cc];
        Vec2 d10 = grid[r * (cols + 1) + (cc + 1)];
        Vec2 d11 = grid[(r + 1) * (cols + 1) + (cc + 1)];
        Vec2 d01 = grid[(r + 1) * (cols + 1) + cc];
        gint qi = r * cols + cc;

        c->qmin_x[qi] = MIN (MIN (d00.x, d10.x), MIN (d11.x, d01.x));
        c->qmax_x[qi] = MAX (MAX (d00.x, d10.x), MAX (d11.x, d01.x));
        c->qmin_y[qi] = MIN (MIN (d00.y, d10.y), MIN (d11.y, d01.y));
        c->qmax_y[qi] = MAX (MAX (d00.y, d10.y), MAX (d11.y, d01.y));
      }

  c->hash = compute_cache_hash (o);
  return c;
}

/* Mutex-protected: guarantees only one thread rebuilds cache at a time,
 * preventing double-free on the old cache and dangling set_data calls. */
static MeshCache *
get_mesh_cache (GeglOperation *operation)
{
  GeglProperties *o = GEGL_PROPERTIES (operation);

  g_mutex_lock (&g_cache_mutex);

  MeshCache *cache = g_object_get_data (G_OBJECT (operation), "mesh-warp-cache");
  guint h = compute_cache_hash (o);

  if (cache && cache->hash == h)
    {
      g_mutex_unlock (&g_cache_mutex);
      return cache;
    }

  if (cache)
    g_object_set_data (G_OBJECT (operation), "mesh-warp-cache", NULL);

  cache = build_mesh_cache (o);
  if (cache)
    g_object_set_data_full (G_OBJECT (operation), "mesh-warp-cache",
                            cache, mesh_cache_free);

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
  GeglProperties *o = GEGL_PROPERTIES (operation);
  const GeglRectangle *src_rect =
    gegl_operation_source_get_bounding_box (operation, "input");

  GeglRectangle in_rect = src_rect ? *src_rect : (GeglRectangle){0, 0, 0, 0};
  GeglRectangle result  = in_rect;

  MeshCache *cache = get_mesh_cache (operation);
  if (!cache) return result;

  gint n_quads = o->cols * o->rows;
  gdouble mnx = cache->qmin_x[0], mxx = cache->qmax_x[0];
  gdouble mny = cache->qmin_y[0], mxy = cache->qmax_y[0];
  for (gint i = 1; i < n_quads; i++)
    {
      if (cache->qmin_x[i] < mnx) mnx = cache->qmin_x[i];
      if (cache->qmax_x[i] > mxx) mxx = cache->qmax_x[i];
      if (cache->qmin_y[i] < mny) mny = cache->qmin_y[i];
      if (cache->qmax_y[i] > mxy) mxy = cache->qmax_y[i];
    }

  gint new_x  = MIN (in_rect.x, (gint) floor (mnx));
  gint new_y  = MIN (in_rect.y, (gint) floor (mny));
  gint new_x2 = MAX (in_rect.x + in_rect.width,  (gint) ceil (mxx));
  gint new_y2 = MAX (in_rect.y + in_rect.height, (gint) ceil (mxy));

  result.x = new_x; result.y = new_y;
  result.width  = new_x2 - new_x;
  result.height = new_y2 - new_y;
  return result;
}

static GeglRectangle
get_required_for_output (GeglOperation       *operation,
                          const gchar         *input_pad,
                          const GeglRectangle *roi)
{
  GeglProperties *o = GEGL_PROPERTIES (operation);
  const GeglRectangle *src_rect =
    gegl_operation_source_get_bounding_box (operation, "input");
  if (!src_rect) return *roi;

  MeshCache *cache = get_mesh_cache (operation);
  if (!cache) return *src_rect;

  gint rq_x2 = roi->x + roi->width;
  gint rq_y2 = roi->y + roi->height;

  gint min_x = G_MAXINT, min_y = G_MAXINT;
  gint max_x = G_MININT, max_y = G_MININT;
  gboolean any = FALSE;

  for (gint r = 0; r < o->rows; r++)
    for (gint cc = 0; cc < o->cols; cc++)
      {
        gint qi = r * o->cols + cc;
        if (cache->qmax_x[qi] < roi->x) continue;
        if (cache->qmin_x[qi] >= rq_x2)  continue;
        if (cache->qmax_y[qi] < roi->y) continue;
        if (cache->qmin_y[qi] >= rq_y2)  continue;

        gint orig_w = o->orig_width  > 0 ? o->orig_width  : src_rect->width;
        gint orig_h = o->orig_height > 0 ? o->orig_height : src_rect->height;

        gint sx0 = o->pad_left + (gint) floor ((gdouble) cc       * orig_w / o->cols);
        gint sx1 = o->pad_left + (gint) ceil  ((gdouble)(cc + 1) * orig_w / o->cols);
        gint sy0 = o->pad_top  + (gint) floor ((gdouble) r        * orig_h / o->rows);
        gint sy1 = o->pad_top  + (gint) ceil  ((gdouble)(r + 1)  * orig_h / o->rows);

        if (sx0 < min_x) min_x = sx0;
        if (sy0 < min_y) min_y = sy0;
        if (sx1 > max_x) max_x = sx1;
        if (sy1 > max_y) max_y = sy1;
        any = TRUE;
      }

  if (!any) return *roi;

  GeglRectangle out;
  out.x = min_x - 1;
  out.y = min_y - 1;
  out.width  = (max_x - min_x) + 3;
  out.height = (max_y - min_y) + 3;
  return out;
}

static gboolean
process (GeglOperation       *operation,
         GeglBuffer          *input,
         GeglBuffer          *output,
         const GeglRectangle *result,
         gint                 level)
{
  GeglProperties *o = GEGL_PROPERTIES (operation);
  const Babl *format = babl_format ("R'G'B'A float");

  const GeglRectangle *src_rect =
    gegl_operation_source_get_bounding_box (operation, "input");
  GeglRectangle in_extent = src_rect ? *src_rect : *result;

  MeshCache *cache = get_mesh_cache (operation);
  if (!cache)
    {
      gegl_buffer_copy (input, result, GEGL_ABYSS_NONE, output, result);
      return TRUE;
    }

  gint cols = o->cols, rows = o->rows;
  gfloat *out_buf = g_new0 (gfloat, (gsize) result->width * result->height * 4);
  GeglSampler *sampler = gegl_buffer_sampler_new (input, format, GEGL_SAMPLER_LINEAR);

  gint rq_x2 = result->x + result->width;
  gint rq_y2 = result->y + result->height;

  /* Source quads must align with the ORIGINAL content region inside the
   * padded input, not with the whole padded canvas. Falls back to full
   * input size if orig_width/orig_height were not provided. */
  gint orig_w = o->orig_width  > 0 ? o->orig_width  : in_extent.width;
  gint orig_h = o->orig_height > 0 ? o->orig_height : in_extent.height;

  gdouble src_x_scale = (gdouble) orig_w / cols;
  gdouble src_y_scale = (gdouble) orig_h / rows;

  for (gint r = 0; r < rows; r++)
    for (gint cc = 0; cc < cols; cc++)
      {
        gint qi = r * cols + cc;

        gint min_x = MAX (result->x, (gint) floor (cache->qmin_x[qi]));
        gint max_x = MIN (rq_x2 - 1,  (gint) ceil  (cache->qmax_x[qi]));
        gint min_y = MAX (result->y, (gint) floor (cache->qmin_y[qi]));
        gint max_y = MIN (rq_y2 - 1,  (gint) ceil  (cache->qmax_y[qi]));
        if (min_x > max_x || min_y > max_y) continue;

        Vec2 d00 = cache->grid[r       * (cols + 1) + cc];
        Vec2 d10 = cache->grid[r       * (cols + 1) + (cc + 1)];
        Vec2 d11 = cache->grid[(r + 1) * (cols + 1) + (cc + 1)];
        Vec2 d01 = cache->grid[(r + 1) * (cols + 1) + cc];

        const gdouble a1 = d00.x;
        const gdouble b1 = d10.x - d00.x;
        const gdouble c1 = d01.x - d00.x;
        const gdouble d1 = d00.x - d10.x + d11.x - d01.x;
        const gdouble a2 = d00.y;
        const gdouble b2 = d10.y - d00.y;
        const gdouble c2 = d01.y - d00.y;
        const gdouble d2 = d00.y - d10.y + d11.y - d01.y;

        const gdouble src_x_base = o->pad_left + cc * src_x_scale;
        const gdouble src_y_base = o->pad_top  + r  * src_y_scale;

        for (gint y = min_y; y <= max_y; y++)
          {
            const gdouble py = (y + 0.5) - a2;
            const gsize row_off = (gsize)(y - result->y) * result->width * 4;

            for (gint x = min_x; x <= max_x; x++)
              {
                const gdouble px = (x + 0.5) - a1;

                const gdouble A = d2 * c1 - c2 * d1;
                const gdouble B = py * d1 - c2 * b1 - d2 * px + b2 * c1;
                const gdouble C = py * b1 - b2 * px;

                gdouble v;
                if (fabs (A) < 1e-9)
                  {
                    if (fabs (B) < 1e-9) v = 0.5;
                    else                 v = -C / B;
                  }
                else
                  {
                    gdouble disc = B * B - 4.0 * A * C;
                    if (disc < 0) continue;
                    gdouble sq = sqrt (disc);
                    gdouble v1 = (-B + sq) / (2.0 * A);
                    gdouble v2 = (-B - sq) / (2.0 * A);
                    if (v1 >= -0.005 && v1 <= 1.005)      v = v1;
                    else if (v2 >= -0.005 && v2 <= 1.005) v = v2;
                    else                                  v = v1;
                  }

                gdouble denom = b1 + d1 * v;
                gdouble u;
                if (fabs (denom) > 1e-7) u = (px - c1 * v) / denom;
                else
                  {
                    gdouble denom_y = b2 + d2 * v;
                    u = (fabs (denom_y) > 1e-7) ? (py - c2 * v) / denom_y : 0.5;
                  }

                if (u < -0.002 || u > 1.002 || v < -0.002 || v > 1.002) continue;
                u = CLAMP (u, 0.0, 1.0);
                v = CLAMP (v, 0.0, 1.0);

                const gdouble src_x = src_x_base + u * src_x_scale;
                const gdouble src_y = src_y_base + v * src_y_scale;

                gfloat pixel[4];
                gegl_sampler_get (sampler, src_x, src_y, NULL, pixel, GEGL_ABYSS_CLAMP);

                const gsize dst_idx = row_off + (gsize)(x - result->x) * 4;
                out_buf[dst_idx + 0] = pixel[0];
                out_buf[dst_idx + 1] = pixel[1];
                out_buf[dst_idx + 2] = pixel[2];
                out_buf[dst_idx + 3] = pixel[3];
              }
          }
      }

  g_object_unref (sampler);
  gegl_buffer_set (output, result, 0, format, out_buf, GEGL_AUTO_ROWSTRIDE);
  g_free (out_buf);
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

#endif
