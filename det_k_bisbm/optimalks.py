import os
import math
import logging
import tempfile
from loky import get_reusable_executor

from det_k_bisbm.utils import *


class OptimalKs(object):
    r"""Base class for OptimalKs.

    Parameters
    ----------
    edgelist : list, required
        Edgelist (bipartite network) for model selection.

    types : list, required
        Types of each node specifying the type membership.

    init_ka : int, required
        Initial Ka for successive merging and searching for the optimum.

    init_kb : int, required
        Initial Ka for successive merging and searching for the optimum.

    i_0 :  double, optional
        Threshold for the merging step (as described in the main text).

    logging_level : str, optional
        Logging level used. It can be one of "warning" or "info".

    """

    def __init__(self,
                 engine,
                 edgelist,
                 types,
                 logging_level="INFO",
                 default_args=True,
                 random_init_k=False,
                 prior_args=None):

        self.engine_ = engine.engine  # TODO: check that engine is an object
        self.max_n_sweeps_ = engine.MAX_NUM_SWEEPS
        self.is_par_ = engine.PARALLELIZATION
        self.n_cores_ = engine.NUM_CORES

        # TODO: "types" is only used to compute na and nb. Can be made more generic.
        self.types = types
        self.n_a = 0
        self.n_b = 0
        self.n = 0
        for _type in types:
            self.n += 1
            if _type in ["1", 1]:
                self.n_a += 1
            elif _type in ["2", 2]:
                self.n_b += 1

        self.edgelist = edgelist
        self.e = len(self.edgelist)
        if engine.ALGM_NAME == "mcmc" and default_args:
            engine.set_steps(self.n * 1e5)
            engine.set_await_steps(self.n * 2e3)
            engine.set_cooling("abrupt_cool")
            engine.set_cooling_param_1(self.n * 1e3)
            engine.set_epsilon(1.)
        if default_args:
            self.ka = int(self.e ** 0.5 / 2)
            self.kb = int(self.e ** 0.5 / 2)
            self.i_0 = 0.1
            self.adaptive_ratio = 0.9  # adaptive parameter to make the "delta" smaller, if it's too large
            self._k_th_nb_to_search = 1
            self._size_rows_to_run = 2
        else:
            self.ka = self.kb = self.i_0 = \
                self.adaptive_ratio = self._k_th_nb_to_search = self._size_rows_to_run = None
        if random_init_k:
            self.ka = np.random.randint(1, self.ka + 1)
            self.kb = np.random.randint(1, self.kb + 1)

        # arguments for setting the prior (TODO)
        self.prior_args = prior_args

        # These confident_* variable are used to store the "true" data
        # that is, not the sloppy temporarily results via matrix merging
        self.bookkeeping_DL = OrderedDict()
        self.bookkeeping_e_rs = OrderedDict()
        self.bookkeeping_profile_likelihood = OrderedDict()

        # These trace_* variable are used to store the data that we temporarily go through
        self.trace_mb = OrderedDict()

        # for debug/temp variables
        self.is_tempfile_existed = True
        self.f_edgelist = tempfile.NamedTemporaryFile(mode='w', delete=False)
        # self.f_edgelist = tempfile.NamedTemporaryFile(mode='w', dir='/scratch/Users/tzye5331/.tmp/', delete=False)
        # To prevent "TypeError: cannot serialize '_io.TextIOWrapper' object" when using loky
        self._f_edgelist_name = self._get_tempfile_edgelist()

        # initialize other class attributes (TODO: what??)
        self.init_italic_i = 0.

        # logging
        self._logger = logging.Logger
        self.set_logging_level(logging_level)
        self._summary = OrderedDict()
        self._summary["init_ka"] = self.ka
        self._summary["init_kb"] = self.kb
        self._summary["na"] = self.n_a
        self._summary["nb"] = self.n_b
        self._summary["e"] = self.e
        self._summary["avg_k"] = 2 * self.e / (self.n_a + self.n_b)

        # look-up tables
        self.__q_cache_f_name = os.path.join(tempfile.mkdtemp(), '__q_cache.dat')  # for restricted integer partitions
        # self.__q_cache_f_name = os.path.join(tempfile.mkdtemp(dir='/scratch/Users/tzye5331/.tmp/'), '__q_cache.dat')
        self.__q_cache = np.array([], ndmin=2)
        self.__q_cache_max_e_r = self.e if self.e <= int(1e4) else int(1e4)

    def _prerunning_checks(self):
        assert self.n_a > 0, "[ERROR] Number of type-a nodes = 0, which is not allowed"
        assert self.n_b > 0, "[ERROR] Number of type-b nodes = 0, which is not allowed"
        assert self.n == self.n_a + self.n_b, \
            "[ERROR] num_nodes ({}) does not equal to num_nodes_a ({}) plus num_nodes_b ({})".format(
                self.n, self.n_a, self.n_b
            )
        if self.ka is None or self.kb is None or self.i_0 is None:
            raise AttributeError("Arguments missing! Please assign `init_ka`, `init_kb`, and `i_0`.")
        if self.adaptive_ratio is None:
            raise AttributeError("Arguments missing! Please assign `adaptive_ratio`.")
        if self._k_th_nb_to_search is None:
            raise AttributeError("Arguments missing! Please assign `k_th_nb_to_search`.")
        if self._size_rows_to_run is None:
            raise AttributeError("Arguments missing! Please assign `size_rows_to_run`.")

    def iterator(self):
        self._prerunning_checks()
        if not self.is_tempfile_existed:
            self._f_edgelist_name = self._get_tempfile_edgelist()

        while self.ka != 1 or self.kb != 1:
            ka_, kb_, m_e_rs_, diff_italic_i, mlist = self._moving_one_step_down(self.ka, self.kb)
            if abs(diff_italic_i) > self.i_0 * self.init_italic_i:
                self._update_current_state(ka_, kb_, m_e_rs_)
                desc_len_, _, _ = self._calc_and_update((self.ka, self.kb))
                if not self._is_mdl_so_far(desc_len_):
                    # merging predicates us to check (ka, kb), however, if it happens to have a higher desc_len
                    # then it is suspected to overshoot.
                    self.i_0 *= self.adaptive_ratio
                    ka_, kb_, _, desc_len_ = self._back_to_where_desc_len_is_lowest()
                is_local_minimum_found = self._check_if_local_minimum(ka_, kb_, desc_len_, self._k_th_nb_to_search)
                if is_local_minimum_found:
                    self._clean_up_and_record_mdl_point()
                    return self.bookkeeping_DL
            else:
                self._update_transient_state(ka_, kb_, m_e_rs_, mlist)

        self._check_if_random_bipartite()
        return self.bookkeeping_DL

    def summary(self):
        ka, kb = sorted(self.bookkeeping_DL, key=self.bookkeeping_DL.get)[0]
        self._summary["ka"] = ka
        self._summary["kb"] = kb
        self._summary["mdl"] = self.bookkeeping_DL[(ka, kb)]
        self._summary["engine_args"] = OrderedDict()
        return self._summary

    def clean(self):
        self.bookkeeping_DL = OrderedDict()
        self.bookkeeping_e_rs = OrderedDict()
        self.bookkeeping_profile_likelihood = OrderedDict()
        self.trace_mb = OrderedDict()
        self.set_params(init_ka=10, init_kb=10, i_0=0.1)

    def compute_and_update(self, ka, kb, recompute=False):
        try:
            os.remove(self._f_edgelist_name)
        except FileNotFoundError as e:
            self._logger.warning("FileNotFoundError: {}".format(e))
        finally:
            self._f_edgelist_name = self._get_tempfile_edgelist()
            q_cache = np.array([], ndmin=2)
            q_cache = init_q_cache(self.__q_cache_max_e_r, q_cache)
            self.set__q_cache(q_cache)  # todo: add some checks here
            if recompute:
                self.bookkeeping_DL[(ka, kb)] = 0
            self._calc_and_update((ka, kb))

    def get_desc_len_from_data(self, na, nb, n_edges, ka, kb, edgelist, mb, diff=False, nr=None, allow_empty=False,
                               partition_dl_kind="distributed", degree_dl_kind="distributed", edge_dl_kind="bipartite"):
        """
        Description length difference to a randomized instance

        Parameters
        ----------
        na: `int`
            Number of nodes in type-a.
        nb: `int`
            Number of nodes in type-b.
        n_edges: `int`
            Number of edges.
        ka: `int`
            Number of communities in type-a.
        kb: `int`
            Number of communities in type-b.
        edgelist: `list`
            Edgelist in Python list structure.
        mb: `list`
            Community membership of each node in Python list structure.
        diff: `bool`
            When `diff == True`,
            the returned description value will be the difference to that of a random bipartite network. Otherwise, it will
            return the entropy (a.k.a. negative log-likelihood) associated with the current block partition.
        allow_empty: `bool`
        nr: `array-like`

        partition_dl_allow_empty: `bool` (optional, default: `False`)
        partition_dl_kind: `str` (optional, default: `"distributed"`)
            1. `partition_dl_kind == "uniform"`
            2. `partition_dl_kind == "distributed"` (default)


        degree_dl_kind: `str` (optional, default: `"distributed"`)
            1. `degree_dl_kind == "uniform"`
            2. `degree_dl_kind == "distributed"` (default)
            3. `degree_dl_kind == "entropy"`

        edge_dl_kind: `str` (optional, default: `"bipartite"`)
            1. `edge_dl_kind == "unipartite"`
            2. `edge_dl_kind == "bipartite"` (default)


        Returns
        -------
        desc_len_b: `float`
            Difference of the description length to the bipartite ER network, per edge.

        """
        edgelist = list(map(lambda e: [int(e[0]), int(e[1])], edgelist))

        italic_i = compute_profile_likelihood(edgelist, mb, ka=ka, kb=kb)
        desc_len = 0.

        # finally, we compute the description length
        if diff:  # todo: add more options to it; now, only uniform prior for P(e) is included.
            desc_len += (na * np.log(ka) + nb * np.log(kb) - n_edges * (italic_i - np.log(2))) / n_edges
            x = float(ka * kb) / n_edges
            desc_len += (1 + x) * np.log(1 + x) - x * np.log(x)
            desc_len -= (1 + 1 / n_edges) * np.log(1 + 1 / n_edges) - (1 / n_edges) * np.log(1 / n_edges)
        else:
            desc_len += adjacency_entropy(edgelist, mb)
            # print("desc len from fitting {}".format(desc_len))
            desc_len += model_entropy(n_edges, ka=ka, kb=kb, na=na, nb=nb, nr=nr,
                                      allow_empty=allow_empty)  # P(e | b) + P(b | K)
            # desc_len += model_entropy(n_edges, k=ka+kb, n=na+nb, nr=nr, allow_empty=allow_empty)  # P(e | b) + P(b | K)
            # P(k |e, b)
            ent = compute_degree_entropy(edgelist, mb, __q_cache=self.__q_cache, degree_dl_kind=degree_dl_kind)
            desc_len += ent
            # print("degree dl = {}".format(ent))
        return desc_len.__float__()

    @staticmethod
    def loky_executor(max_workers, timeout, func, feeds):
        assert type(feeds) is list, "[ERROR] feeds should be a Python list; here it is {}".format(str(type(feeds)))
        loky_executor = get_reusable_executor(max_workers=int(max_workers), timeout=int(timeout))
        results = loky_executor.map(func, feeds)
        return results

    @staticmethod
    def _calc_entropy_edge_counts(self):
        pass

    @staticmethod
    def _calc_entropy_node_degree(self):
        pass

    @staticmethod
    def _h_func(x):
        return (1 + x) * math.log(1 + x) - x * math.log(x)

    def _calc_with_hook(self, ka, kb, old_desc_len=None):
        """
        Execute the partitioning code by spawning child processes in the shell; save its output afterwards.

        Parameters
        ----------
        ka : int
            Number of type-a communities that one wants to partition on the bipartite graph
        kb : int
            Number of type-b communities that one wants to partition on the bipartite graph

        Returns
        -------
        italic_i : float
            the profile likelihood of the found partition

        m_e_rs : numpy array
            the affinity matrix via the group membership vector found by the partitioning engine

        mb : list[int]
            group membership vector calculated by the partitioning engine

        """
        # each time when you calculate/search at particular ka and kb
        # the hood records relevant information for research
        try:
            self.bookkeeping_DL[(ka, kb)]
        except KeyError as _:
            pass
        else:
            if self.bookkeeping_DL[(ka, kb)] != 0:
                italic_i = self.bookkeeping_profile_likelihood[(ka, kb)]
                m_e_rs = self.bookkeeping_e_rs[(ka, kb)]
                mb = self.trace_mb[(ka, kb)]
                self._logger.info("... fetch calculated data ...")
                return italic_i, m_e_rs, mb

        def run(ka, kb):
            mb = self.engine_(self._f_edgelist_name, self.n_a, self.n_b, ka, kb)
            return mb

        # Calculate the biSBM inference several times,
        # choose the maximum likelihood (or minimum entropy) result.
        results = []
        if old_desc_len is None:
            if self.is_par_:
                # automatically shutdown after idling for 60s
                results = list(
                    self.loky_executor(self.n_cores_, 60, lambda x: run(ka, kb), list(range(self.max_n_sweeps_)))
                )
            else:
                results = [run(ka, kb)]
        else:
            old_desc_len = float(old_desc_len)
            if self.is_par_:
                self.__q_cache = np.array([], ndmin=2)
                results = list(
                    self.loky_executor(self.n_cores_, 60, lambda x: run(ka, kb), list(range(self.max_n_sweeps_)))
                )
            else:
                # if old_desc_len is passed
                # we compare the new_desc_len with the old one
                # --
                # this option is used when we want to decide whether
                # we should escape from the local minimum during the heuristic
                calculate_times = 0
                while calculate_times < self.max_n_sweeps_:
                    result = run(ka, kb)
                    results.append(result)
                    # new_desc_len = self._cal_desc_len(ka, kb, result[1])
                    nr = get_n_r_from_mb(result)
                    new_desc_len = self.get_desc_len_from_data(
                        self.n_a, self.n_b, self.e, ka, kb, list(self.edgelist), result, nr=nr)
                    if new_desc_len < old_desc_len:
                        # no need to go further
                        calculate_times = self.max_n_sweeps_
                    else:
                        calculate_times += 1

        max_e_r = self.__q_cache_max_e_r
        if old_desc_len is None and len(self.__q_cache) == 1:
            fp = np.memmap(self.__q_cache_f_name, dtype='uint32', mode="w+", shape=(max_e_r + 1, max_e_r + 1))
            self.__q_cache = init_q_cache(max_e_r, np.array([], ndmin=2))
            fp[:] = self.__q_cache[:]
            del fp
        else:
            self.__q_cache = np.memmap(self.__q_cache_f_name, dtype='uint32', mode='r', shape=(max_e_r + 1, max_e_r + 1))

        result_ = [self.__compute_desc_len(
            self.n_a, self.n_b, self.e, ka, kb, r
        ) for r in results]
        result = min(result_, key=lambda x: x[3])
        italic_i = result[0]
        m_e_rs = result[1]
        mb = result[2]
        return italic_i, m_e_rs, mb

    def __compute_desc_len(self, n_a, n_b, e, ka, kb, mb):
        m_e_rs, _ = get_m_e_rs_from_mb(self.edgelist, mb)
        italic_i = compute_profile_likelihood_from_e_rs(m_e_rs)
        nr = get_n_r_from_mb(mb)
        desc_len = self.get_desc_len_from_data(n_a, n_b, e, ka, kb, list(self.edgelist), mb, nr=nr)
        return italic_i, m_e_rs, mb, desc_len

    def _moving_one_step_down(self, ka, kb):
        """
        Apply multiple merges of the original affinity matrix, return the one that least alters the entropy

        Parameters
        ----------
        ka : int
            number of type-a communities in the affinity matrix
        kb : int
            number of type-b communities in the affinity matrix

        Returns
        -------
        _ka : int
            the new number of type-a communities in the affinity matrix

        _kb : int
            the new number of type-b communities in the affinity matrix

        _m_e_rs : numpy array
            the new affinity matrix

        diff_italic_i : list(int, int)
            the difference of the new profile likelihood and the old one

        _mlist : list(int, int)
            the two row-indexes of the original affinity matrix that were finally chosen (and merged)

        """
        if self.init_italic_i == 0:
            # This is an important step, where we calculate the graph partition at init (ka, kb)
            _, m_e_rs, italic_i = self._calc_and_update((ka, kb))

            self.init_italic_i = italic_i
            self.m_e_rs = m_e_rs

        def _sample_and_merge():
            _ka, _kb, _m_e_rs, _mlist = merge_matrix(self.ka, self.kb, self.m_e_rs)
            _italic_I = compute_profile_likelihood_from_e_rs(_m_e_rs)
            diff_italic_i = _italic_I - self.init_italic_i  # diff_italic_i is always negative;
            return _ka, _kb, _m_e_rs, diff_italic_i, _mlist

        # how many times that a sample merging takes place (todo: better description??)
        indexes_to_run_ = range(0, (ka + kb) * self._size_rows_to_run)

        results = []
        for _ in indexes_to_run_:
            results.append(_sample_and_merge())

        _ka, _kb, _m_e_rs, _diff_italic_i, _mlist = max(results, key=lambda x: x[3])

        assert int(_m_e_rs.sum()) == int(self.e * 2), "__m_e_rs.sum() = {}; self.e * 2 = {}".format(
            str(int(_m_e_rs.sum())), str(self.e * 2)
        )

        return _ka, _kb, _m_e_rs, _diff_italic_i, _mlist

    def _check_if_random_bipartite(self):
        # if we reached (1, 1), check that it's the local optimal point, then we could return (1, 1).
        points_to_compute = [(1, 1), (1, 2), (2, 1), (2, 2)]
        for point in points_to_compute:
            self.compute_and_update(point[0], point[1], recompute=True)
        p_estimate = sorted(self.bookkeeping_DL, key=self.bookkeeping_DL.get)[0]

        if p_estimate != (1, 1):
            # TODO: write some documentation here
            raise UserWarning("[WARNING] merging reached (1, 1); cannot go any further, please set a smaller <i_0>.")
        self._clean_up_and_record_mdl_point()

    def _update_transient_state(self, ka_moving, kb_moving, t_m_e_rs, mlist):
        old_of_g = self.trace_mb[(self.ka, self.kb)]
        new_of_g = list(np.zeros(self.n))

        mlist.sort()
        for _node_id, _g in enumerate(old_of_g):
            if _g == mlist[1]:
                new_of_g[_node_id] = mlist[0]
            elif _g < mlist[1]:
                new_of_g[_node_id] = _g
            else:
                new_of_g[_node_id] = _g - 1
        assert max(new_of_g) + 1 == ka_moving + kb_moving, \
            "[ERROR] inconsistency between the membership indexes and the number of blocks."
        self.trace_mb[(ka_moving, kb_moving)] = new_of_g
        self._update_current_state(ka_moving, kb_moving, t_m_e_rs)

    def _check_if_local_minimum(self, ka, kb, old_desc_len, k_th):
        """The `neighborhood search` as described in the paper."""
        self.is_tempfile_existed = True
        nb_points = map(lambda x: (x[0] + ka, x[1] + kb), product(range(-k_th, k_th + 1), repeat=2))
        # if any item has values less than 1, delete it. Also, exclude the suspected point (i.e., [ka, kb]).
        nb_points = [(i, j) for i, j in nb_points if i >= 1 and j >= 1 and (i, j) != (ka, kb)]
        ka_moving, kb_moving = 0, 0

        for point in nb_points:
            self._calc_and_update(point, old_desc_len)
            if self._is_mdl_so_far(self.bookkeeping_DL[(point[0], point[1])]):
                p_estimate = sorted(self.bookkeeping_DL, key=self.bookkeeping_DL.get)[0]
                self._logger.info("Found {} that gives an even lower description length ...".format(p_estimate))
                ka_moving, kb_moving, _, _ = self._back_to_where_desc_len_is_lowest()
                break
        if ka_moving * kb_moving == 0:
            return True
        else:
            return False

    def _clean_up_and_record_mdl_point(self):
        try:
            os.remove(self._f_edgelist_name)
        except FileNotFoundError as e:
            self._logger.warning("FileNotFoundError: {}".format(e))
        finally:
            self.is_tempfile_existed = False
            p_estimate = sorted(self.bookkeeping_DL, key=self.bookkeeping_DL.get)[0]
            self._logger.info("DONE: the MDL point is {}".format(p_estimate))
            os.remove(self.__q_cache_f_name)

    def _is_mdl_so_far(self, desc_len):
        """Check if `desc_len` is the minimal value so far."""
        return not any([i < desc_len for i in self.bookkeeping_DL.values()])

    def _back_to_where_desc_len_is_lowest(self):
        ka = sorted(self.bookkeeping_DL, key=self.bookkeeping_DL.get, reverse=False)[0][0]
        kb = sorted(self.bookkeeping_DL, key=self.bookkeeping_DL.get, reverse=False)[0][1]
        m_e_rs = self.bookkeeping_e_rs[(ka, kb)]
        self._update_current_state(ka, kb, m_e_rs)
        return ka, kb, m_e_rs, self.bookkeeping_DL[(self.ka, self.kb)]

    def _update_current_state(self, ka, kb, m_e_rs):
        self.ka = ka
        self.kb = kb
        self.m_e_rs = m_e_rs  # this will be used in _moving_one_step_down function

    def _calc_and_update(self, point, old_desc_len=0.):
        self._logger.info("Now computing graph partition at {} ...".format(point))
        if old_desc_len == 0.:
            italic_i, m_e_rs, mb = self._calc_with_hook(point[0], point[1], old_desc_len=None)
        else:
            italic_i, m_e_rs, mb = self._calc_with_hook(point[0], point[1], old_desc_len=old_desc_len)
        # candidate_desc_len = self._cal_desc_len(point[0], point[1], italic_i)
        nr = get_n_r_from_mb(mb)
        candidate_desc_len = self.get_desc_len_from_data(
            self.n_a, self.n_b, self.e, point[0], point[1], list(self.edgelist), mb, nr=nr)

        self.bookkeeping_DL[point] = candidate_desc_len
        self.bookkeeping_profile_likelihood[point] = italic_i
        self.bookkeeping_e_rs[point] = m_e_rs
        assert max(mb) + 1 == point[0] + point[1], "[ERROR] inconsistency between mb. indexes and #blocks."
        self.trace_mb[point] = mb
        self._logger.info("... DONE.")

        # update the predefined threshold value, DELTA:
        self.init_italic_i = italic_i

        return candidate_desc_len, m_e_rs, italic_i

    def _get_tempfile_edgelist(self):
        try:
            self.f_edgelist.seek(0)
        except AttributeError:
            self.f_edgelist = tempfile.NamedTemporaryFile(mode='w', delete=False)
        finally:
            for edge in self.edgelist:
                self.f_edgelist.write(str(edge[0]) + "\t" + edge[1] + "\n")
            self.f_edgelist.flush()
            f_edgelist_name = self.f_edgelist.name
            del self.f_edgelist
        return f_edgelist_name

    #
    #  Set & Get of parameters
    #
    def set_logging_level(self, level):
        _level = 0
        if level.upper() == "INFO":
            _level = logging.INFO
        elif level.upper() == "WARNING":
            _level = logging.WARNING
        logging.basicConfig(
            level=_level,
            format="%(asctime)s:%(levelname)s:%(message)s"
        )
        self._logger = logging.getLogger(__name__)

    def set_params(self, init_ka=10, init_kb=10, i_0=0.1):
        # params for the heuristic
        self.ka = int(init_ka)
        self.kb = int(init_kb)
        self.i_0 = float(i_0)
        assert 0. <= self.i_0 < 1, "[ERROR] Allowed range for i_0 is [0, 1)."
        assert self.ka <= self.n_a, "[ERROR] Number of type-a communities must be smaller than the # nodes in type-a."
        assert self.kb <= self.n_b, "[ERROR] Number of type-b communities must be smaller than the # nodes in type-b."
        self._summary["init_ka"] = self.ka
        self._summary["init_kb"] = self.kb

    def set_adaptive_ratio(self, adaptive_ratio):
        self.adaptive_ratio = float(adaptive_ratio)

    def set_k_th_neighbor_to_search(self, k):
        self._k_th_nb_to_search = int(k)

    def set_size_rows_to_run(self, s):
        self._size_rows_to_run = int(s)

    def get__q_cache(self):
        return self.__q_cache

    def set__q_cache(self, q_cache):
        self.__q_cache = q_cache
        fp = np.memmap(self.__q_cache_f_name, dtype='uint32', mode="w+", shape=(q_cache.shape[0], q_cache.shape[1]))
        fp[:] = self.__q_cache[:]
        del fp
