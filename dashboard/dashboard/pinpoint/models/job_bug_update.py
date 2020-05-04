# Copyright 2020 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Logic for posting Job updates to the issue tracker."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from collections import namedtuple

from dashboard import update_bug_with_results
from dashboard.common import utils
from dashboard.services import issue_tracker_service
from dashboard.models import histogram
from dashboard.pinpoint.models import job_state

from tracing.value.diagnostics import reserved_infos


_INFINITY = u'\u221e'
_RIGHT_ARROW = u'\u2192'
_ROUND_PUSHPIN = u'\U0001f4cd'


class DifferencesFoundBugUpdateBuilder(object):
  """Builder for bug updates about differences found in a metric.

  Accumulate the found differences into this with AddDifference(), then call
  BuildUpdate() to get the bug update to send.

  So intended usage looks like::

    builder = DifferencesFoundBugUpdateBuilder()
    for d in FindDifferences():
      ...
      builder.AddDifference(d.commit, d.values_a, d.values_b)
    issue_update_info = builder.BuildUpdate(tags, url)
  """

  def __init__(self, metric):
    self._metric = metric
    self._differences = []

  def AddDifference(self, commit, values_a, values_b):
    """Add a difference (a commit where the metric changed significantly).

    Args:
      commit: a Commit.
      values_a: (list) result values for the prior commit.
      values_b: (list) result values for this commit.
    """
    commit_info = commit.AsDict()
    self._differences.append(_Difference(commit_info, values_a, values_b))

  def BuildUpdate(self, tags, url):
    """Return _BugUpdateInfo for the differences."""
    if len(self._differences) == 0:
      raise ValueError("BuildUpdate called with 0 differences")
    owner, cc_list, notify_why_text = self._PeopleToNotify()
    comment_text = _FormatComment(self._differences, self._metric,
                                  notify_why_text, tags, url)
    labels = [
        'Pinpoint-Culprit-Found'
        if len(self._differences) == 1 else 'Pinpoint-Multiple-Culprits'
    ]
    return _BugUpdateInfo(comment_text, owner, cc_list, labels)

  def GenerateCommitCacheKey(self):
    commit_cache_key = None
    if len(self._differences) == 1:
      commit_cache_key = update_bug_with_results._GetCommitHashCacheKey(
          self._differences[0].commit_info.get('git_hash'))
    return commit_cache_key

  def _OrderedCommitsByDelta(self):
    """Return the list of commits sorted by absolute change."""
    commits_with_deltas = [(diff.MeanDelta(), diff.commit_info)
                           for diff in self._differences
                           if diff.values_a and diff.values_b]
    return [
        commit for _, commit in sorted(
            commits_with_deltas, key=lambda i: abs(i[0]), reverse=True)
    ]

  def _PeopleToNotify(self):
    """Return the people to notify for these differences.

    This looks at the top commits (by absolute change), and returns a tuple of:
      * owner (str, will be ignored if the bug is already assigned)
      * cc_list (list, authors of the top 2 commits)
      * why_text (str, text explaining why this owner was chosen)
    """
    ordered_commits = self._OrderedCommitsByDelta()[:2]

    # CC the folks in the top two commits.
    cc_list = set()
    for commit in ordered_commits:
      cc_list.add(commit['author'])

    # Assign to the author of the top commit.  If that is an autoroll, assign to
    # a sheriff instead.
    why_text = ''
    top_commit = ordered_commits[0]
    owner = top_commit['author']
    sheriff = utils.GetSheriffForAutorollCommit(owner, top_commit['message'])
    if sheriff:
      owner = sheriff
      why_text = '\n\nAssigning to sheriff %s because "%s" is a roll.' % (
          sheriff, top_commit['subject'])

    return owner, cc_list, why_text


class _Difference(object):

  def __init__(self, commit_info, values_a, values_b):
    self.commit_info = commit_info
    self.values_a = values_a
    self.values_b = values_b

  def MeanDelta(self):
    return job_state.Mean(self.values_b) - job_state.Mean(self.values_a)

  def FormatForBug(self, metric):
    subject = '<b>%s</b> by %s' % (
        self.commit_info['subject'], self.commit_info['author'])

    if self.values_a:
      mean_a = job_state.Mean(self.values_a)
      formatted_a = '%.4g' % mean_a
    else:
      mean_a = None
      formatted_a = 'No values'

    if self.values_b:
      mean_b = job_state.Mean(self.values_b)
      formatted_b = '%.4g' % mean_b
    else:
      mean_b = None
      formatted_b = 'No values'

    metric = '%s: ' % metric if metric else ''

    difference = '%s%s %s %s' % (metric, formatted_a, _RIGHT_ARROW, formatted_b)
    if self.values_a and self.values_b:
      difference += ' (%+.4g)' % (mean_b - mean_a)
      if mean_a:
        difference += ' (%+.4g%%)' % ((mean_b - mean_a) / mean_a * 100)
      else:
        difference += ' (+%s%%)' % _INFINITY

    return '\n'.join((subject, self.commit_info['url'], difference))


class _BugUpdateInfo(
    namedtuple('_BugUpdateInfo',
               ['comment_text', 'owner', 'cc_list', 'labels'])):
  """An update to post to a bug.

  This is the return type of DifferencesFoundBugUpdateBuilder.BuildUpdate.
  """


def _FormatComment(differences, metric, notify_why_text, tags, url):
  if len(differences) == 1:
    status = 'Found a significant difference after 1 commit.'
  else:
    status = ('Found significant differences after each of %d commits.' %
              len(differences))

  title = '<b>%s %s</b>' % (_ROUND_PUSHPIN, status)
  header = '\n'.join((title, url))
  body = '\n\n'.join(diff.FormatForBug(metric) for diff in differences)
  body += notify_why_text

  footer = ('Understanding performance regressions:\n'
            '  http://g.co/ChromePerformanceRegressions')

  if differences:
    footer += _FormatDocumentationUrls(tags)

  comment = '\n\n'.join((header, body, footer))
  return comment


def _ComputePostMergeDetails(issue_tracker, commit_cache_key, cc_list):
  merge_details = {}
  if commit_cache_key:
    merge_details = update_bug_with_results.GetMergeIssueDetails(
        issue_tracker, commit_cache_key)
    if merge_details['id']:
      cc_list = []
  return merge_details, cc_list


def _GetBugStatus(issue_tracker, bug_id):
  if not bug_id:
    return None

  issue_data = issue_tracker.GetIssue(bug_id)
  if not issue_data:
    return None

  return issue_data.get('status')


def _FormatDocumentationUrls(tags):
  if not tags:
    return ''

  # TODO(simonhatch): Tags isn't the best way to get at this, but wait until
  # we move this back into the dashboard so we have a better way of getting
  # at the test path.
  # crbug.com/876899
  test_path = tags.get('test_path')
  if not test_path:
    return ''

  test_suite = utils.TestKey('/'.join(test_path.split('/')[:3]))
  docs = histogram.SparseDiagnostic.GetMostRecentDataByNamesSync(
      test_suite, [reserved_infos.DOCUMENTATION_URLS.name])

  if not docs:
    return ''

  docs = docs[reserved_infos.DOCUMENTATION_URLS.name].get('values')
  footer = '\n\n%s:\n  %s' % (docs[0][0], docs[0][1])
  return footer


def UpdatePostAndMergeDeferred(bug_update_builder, bug_id, tags, url):
  if not bug_id:
    return
  commit_cache_key = bug_update_builder.GenerateCommitCacheKey()
  bug_update = bug_update_builder.BuildUpdate(tags, url)
  issue_tracker = issue_tracker_service.IssueTrackerService(
      utils.ServiceAccountHttp())
  merge_details, cc_list = _ComputePostMergeDetails(issue_tracker,
                                                    commit_cache_key,
                                                    bug_update.cc_list)
  current_bug_status = _GetBugStatus(issue_tracker, bug_id)
  if not current_bug_status:
    return

  status = None
  bug_owner = None

  if current_bug_status in ['Untriaged', 'Unconfirmed', 'Available']:
    # Set the bug status and owner if this bug is opened and unowned.
    status = 'Assigned'
    bug_owner = bug_update.owner

  issue_tracker.AddBugComment(
      bug_id,
      bug_update.comment_text,
      status=status,
      cc_list=sorted(cc_list),
      owner=bug_owner,
      labels=bug_update.labels,
      merge_issue=merge_details.get('id'))
  update_bug_with_results.UpdateMergeIssue(commit_cache_key, merge_details,
                                           bug_id)