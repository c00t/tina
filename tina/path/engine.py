from ..advans import *


@ti.data_oriented
class PathEngine:
    def __init__(self, geom, lighting, mtltab, res=512):
        if isinstance(res, int): res = res, res
        self.res = ti.Vector(res)
        self.nrays = self.res.x * self.res.y

        self.img = ti.Vector.field(3, float, self.res)
        self.cnt = ti.field(int, self.res)

        self.geom = geom
        self.lighting = lighting
        self.mtltab = mtltab
        self.stack = tina.Stack(N_mt=self.nrays)

        self.W2V = ti.Matrix.field(4, 4, float, ())
        self.V2W = ti.Matrix.field(4, 4, float, ())
        self.uniqid = ti.field(int, ())

        @ti.materialize_callback
        @ti.kernel
        def init_engine():
            self.W2V[None] = ti.Matrix.identity(float, 4)
            self.W2V[None][2, 2] = -1
            self.V2W[None] = ti.Matrix.identity(float, 4)
            self.V2W[None][2, 2] = -1

    def clear_image(self):
        self.img.fill(0)
        self.cnt.fill(0)

    @ti.func
    def _f_get_image(self, out: ti.template(),
                     tonemap: ti.template(), is_ext: ti.template()):
        for I in ti.grouped(self.img):
            val = lerp((I // 8).sum() % 2, V(.4, .4, .4), V(.9, .9, .9))
            if self.cnt[I] != 0:
                val = self.img[I] / self.cnt[I]
            if not all(val >= 0 or val <= 0):  # NaN?
                val = V(.9, .4, .9)
            val = tonemap(val)
            if ti.static(is_ext):
                for k in ti.static(range(3)):
                    out[I, k] = val[k]
            else:
                out[I] = val

    @ti.kernel
    def _get_image_e(self, out: ti.ext_arr(), notone: ti.template()):
        tonemap = ti.static((lambda x: x) if notone else aces_tonemap)
        self._f_get_image(out, tonemap, True)

    @ti.kernel
    def _get_image_f(self, out: ti.template()):
        self._f_get_image(out, lambda x: x, False)

    def get_image(self, out=None, notone=False):
        if out is None:
            out = np.zeros((*self.res, 3), dtype=np.float32)
            self._get_image_e(out, notone)
        else:
            self._get_image_f(out)
        return out

    @ti.kernel
    def trace_rays(self, maxdepth: int, surviverate: float):
        self.uniqid[None] += 1
        for _ in ti.smart(self.stack):
            I = V(_ // self.res.x, _ % self.res.x)
            ro, rd, rc, rl, rw = self.generate_ray(I)

            rng = tina.TaichiRNG()
            for depth in range(maxdepth):
                ro, rd, rc, rl, rw = self.transmit_ray(ro, rd, rc, rl, rw, rng)
                rate = lerp(ti.tanh(Vavg(rc) * surviverate), 0.04, 0.95)
                if ti.random() >= rate:
                    rc *= 0
                else:
                    rc /= rate
                if not Vany(rc > 0):
                    break

            #if strict and Vany(rc > 0):
            #    continue
            self.img[I] += rl * tina.wav_to_rgb(rw)
            self.cnt[I] += 1

    @ti.func
    def generate_ray(self, I):
        bias = ti.Vector([ti.random(), ti.random()])
        uv = (I + bias) / self.res * 2 - 1
        ro = mapply_pos(self.V2W[None], V(uv.x, uv.y, -1.0))
        ro1 = mapply_pos(self.V2W[None], V(uv.x, uv.y, +1.0))
        rd = (ro1 - ro).normalized()
        rw = tina.random_wav(self.uniqid[None] + I.y)
        rc = 1.0
        rl = 0.0
        return ro, rd, rc, rl, rw

    @ti.kernel
    def trace_lays(self, maxdepth: int, surviverate: float):
        for _ in ti.smart(self.stack):
            ro, rd, rc, rw = self.generate_lay()

            rng = tina.TaichiRNG()
            for depth in range(maxdepth):
                ro, rd, rc, rw = self.transmit_lay(ro, rd, rc, rw, rng)
                rate = lerp(ti.tanh(Vavg(rc) * surviverate), 0.04, 0.95)
                if ti.random() >= rate:
                    rc *= 0
                else:
                    rc /= rate
                if not Vany(rc > 0):
                    break

    @ti.func
    def generate_lay(self):
        ind = ti.random(int) % self.lighting.get_nlights()
        ro, rd = self.lighting.emit_light(ind)
        rw = tina.random_wav(ti.random(int))
        rc = 1.0
        return ro, rd, rc, rw

    @ti.func
    def update_image_light(self, uv, rc, rw):
        I = ifloor((uv * 0.5 + 0.5) * self.res)
        self.img[I] += rc * tina.wav_to_rgb(rw)
        self.cnt[I] += 1

    def set_camera(self, view, proj):
        W2V = proj @ view
        V2W = np.linalg.inv(W2V)
        self.W2V.from_numpy(np.array(W2V, dtype=np.float32))
        self.V2W.from_numpy(np.array(V2W, dtype=np.float32))

    @ti.func
    def transmit_ray(self, ro, rd, rc, rl, rw, rng):
        near, ind, gid, uv = self.geom.hit(ro, rd)
        if gid == -1:
            # no hit
            rl += rc * self.lighting.background(rd, rw)
            rc *= 0
        else:
            # hit object
            ro += near * rd
            nrm, tex = self.geom.calc_geometry(near, gid, ind, uv, ro, rd)

            sign = 1
            if nrm.dot(rd) > 0:
                sign = -1
                nrm = -nrm

            tina.Input.spec_g_pars({
                'pos': ro,
                'color': 1.,
                'normal': nrm,
                'texcoord': tex,
            })

            mtlid = self.geom.get_material_id(ind, gid)
            material = self.mtltab.get(mtlid)

            ro += nrm * eps * 8

            li_clr = 0.
            for li_ind in range(self.lighting.get_nlights()):
                # cast shadow ray to lights
                new_rd, li_wei, li_dis = self.lighting.redirect(ro, li_ind, rw)
                li_wei *= max(0, new_rd.dot(nrm))
                if Vall(li_wei <= 0):
                    continue
                occ_near, occ_ind, occ_gid, occ_uv = self.geom.hit(ro, new_rd)
                if occ_gid != -1 and occ_near < li_dis:  # shadow occlusion
                    continue  # but what if it's glass?
                li_wei *= material.wav_brdf(nrm, -rd, new_rd, rw)
                li_clr += li_wei

            # sample indirect light
            rd, ir_wei = material.wav_sample(-rd, nrm, sign, rng, rw)
            if rd.dot(nrm) < 0:
                # refract into / outof
                ro -= nrm * eps * 16

            tina.Input.clear_g_pars()

            rl += rc * li_clr
            rc *= ir_wei

        return ro, rd, rc, rl, rw

    @ti.func
    def transmit_lay(self, ro, rd, rc, rw, rng):
        near, ind, gid, uv = self.geom.hit(ro, rd)
        if gid == -1:
            # no hit
            rc *= 0
        else:
            # hit object
            ro += near * rd
            nrm, tex = self.geom.calc_geometry(near, gid, ind, uv, ro, rd)

            sign = 1
            if nrm.dot(rd) > 0:
                sign = -1
                nrm = -nrm

            tina.Input.spec_g_pars({
                'pos': ro,
                'color': 1.0,
                'normal': nrm,
                'texcoord': tex,
            })

            mtlid = self.geom.get_material_id(ind, gid)
            material = self.mtltab.get(mtlid)

            ro += nrm * eps * 8

            # cast shadow ray to camera
            vpos = mapply_pos(self.W2V[None], ro)
            if all(-1 < vpos <= 1):
                vpos.z = -1.0
                ro0 = mapply_pos(self.V2W[None], vpos)
                new_rd = (ro0 - ro).normalized()
                li_dis = (ro0 - ro).norm()
                li_clr = rc * max(0, -rd.dot(nrm))
                if Vany(li_clr > 0):
                    occ_near, occ_ind, occ_gid, occ_uv = self.geom.hit(ro, new_rd)
                    if occ_gid == -1 or occ_near >= li_dis:  # no shadow occlusion
                        li_clr *= material.wav_brdf(nrm, -rd, new_rd, rw)
                        self.update_image_light(vpos.xy, li_clr, rw)

            # sample indirect light
            rd, ir_wei = material.wav_sample(-rd, nrm, sign, rng, rw)
            if rd.dot(nrm) < 0:
                # refract into / outof
                ro -= nrm * eps * 16

            tina.Input.clear_g_pars()

            rc *= ir_wei

        return ro, rd, rc, rw
