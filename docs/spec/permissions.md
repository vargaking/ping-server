# Roles, permissions and channel overwrites

What the app does today, one behaviour per bullet, and the tests that cover it.
Marked *(open)* where an open decision may change it.

Test names are short: server tests are `file::test` under `tests/` in this repo;
`fe:` is a frontend unit test file, `e2e:` a frontend Playwright spec (ping-frontend).
"No test" means nothing covers it.

## Roles

- **PERM-01** A new server has one role, @everyone (View, Send, Connect, Speak, Stream, Create Invite).
  - permissions::test_a_new_server_has_only_everyone
- **PERM-02** Any member can read the role list and the member list (owner first, assigned role ids, never @everyone). Outsiders get 404 or 403, never data.
  - permissions::test_roles_are_hidden_from_non_members, test_non_members_get_403_or_404_never_data, test_members_list_shows_assigned_roles_only; members::test_list_members_returns_owner_first_with_join_dates
- **PERM-03** A new role lands just above @everyone and pushes the others up. Names are trimmed, 1–50 characters, unique in the server. A server holds at most 100 roles.
  - roles_api::test_new_role_lands_at_position_one_and_the_others_shift_up, test_create_accepts_initial_fields, test_create_refuses_a_taken_name, test_create_rejects_a_bad_body, test_a_server_holds_at_most_one_hundred_roles
- **PERM-04** Role fields are validated the same way on create and edit: colour `#rrggbb` or none, known bits only, no bit both allowed and denied, a parent from the same server. Each field can be changed alone.
  - roles_api::test_create_rejects_a_bad_body, test_create_accepts_integer_permissions_too, test_parent_from_another_server_is_rejected, test_patch_changes_each_field, test_patch_rejects_a_bad_body, test_patch_refuses_a_taken_name_and_an_unknown_role
- **PERM-05** A role can inherit from one parent. A loop is refused ("That would make X inherit from itself"). Deleting a parent keeps its children with their own bits.
  - roles_api::test_a_cycle_is_rejected_with_a_message, test_deleting_a_parent_leaves_children_with_their_own_bits; fe: roles.test (wouldCycle, mirror)
- **PERM-06** @everyone can't be renamed, deleted, given a parent or a deny; editing its allow changes everyone at once.
  - roles_api::test_everyone_rules, test_editing_everyone_changes_every_members_mask
- **PERM-07** Reordering lists every role except @everyone once, highest first, or nothing moves.
  - roles_api::test_reorder_sets_positions_top_first, test_reorder_needs_every_non_default_role_exactly_once; fe: roles.test (ordering)
- **PERM-08** Deleting a role takes it from everyone who had it, deletes the channel rules naming it, and closes the gap in ranks.
  - roles_api::test_deleting_a_role_removes_its_assignments; channel_permissions::test_kick_removes_the_members_rows_and_role_delete_cascades
- **PERM-09** A member's roles are replaced as a whole set. @everyone is implicit; the owner's roles can't be changed.
  - permissions::test_role_assignment_rules; e2e: member-roles.spec (the one cross-stack flow)

## Working out a member's permissions

- **PERM-10** Server permissions are @everyone's, then each held role from lowest rank to highest; for each bit the highest role that says anything decides. Unrelated roles add up; a deny on @everyone does nothing.
  - role_resolution::test_muted_is_decided_by_rank, test_unrelated_roles_add_up, test_default_role_deny_is_ignored_and_unassigned_roles_do_not_apply; roles_api::test_a_role_grants_its_bits, test_muted_above_and_below_a_role_that_allows_the_bit; fe: roles.test (memberMask, applyLayer — a hand-copied mirror)
- **PERM-11** Inheritance resolves nearest first. Old data can't break it: a loop stops, a bit both allowed and denied counts as denied, unknown bits drop, equal ranks apply in id order.
  - role_resolution::test_the_nearer_role_decides, test_grandparent_value_survives_when_nothing_nearer_says_anything, test_a_parent_cycle_in_old_data_terminates, test_a_bit_in_both_allow_and_deny_of_one_role_counts_as_deny, test_mask_is_limited_to_known_bits, test_equal_positions_are_applied_in_id_order; roles_api::test_a_child_inherits_and_its_deny_wins; fe: roles.test (resolveRole, mirror)

## Who may change roles

- **PERM-12** Role changes need Manage Roles. The owner skips every rule. Anyone else can only touch roles strictly below their highest role ("That role is at or above your highest role").
  - roles_api::test_roles_need_manage_roles, test_a_member_below_a_role_cannot_touch_it, test_a_member_can_create_and_manage_a_role_below_their_rank, test_a_member_cannot_move_a_role_to_or_above_their_rank, test_a_member_can_reorder_roles_below_their_rank, test_the_owner_skips_every_rule, test_a_member_without_a_role_cannot_create_one, test_assigning_a_role_at_or_above_your_rank_is_refused; fe: roles.test (rankOf, canTouchRole, canAssignRole — editor guards)
- **PERM-13** A non-owner can't grant, edit or inherit from a role holding bits they lack ("You can't change permissions you don't have"). This keeps the danger-zone bits (Delete Channels, Match Imported Authors, Manage Server) from anyone who lacks them. Someone who holds one can give it to @everyone.
  - roles_api::test_a_member_cannot_switch_on_a_bit_they_do_not_hold, test_a_member_cannot_rename_a_role_holding_bits_they_lack, test_a_member_cannot_inherit_from_admin, test_assigning_a_role_that_inherits_admin_needs_admins_bits
- **PERM-14** To edit or delete a role a non-owner must outrank everything that inherits from it.
  - roles_api::test_editing_a_role_needs_to_outrank_everything_that_inherits_from_it

## Server permissions

- **PERM-15** The owner has every permission, sees every channel, and can't be given roles, kicked, muted or disconnected.
  - permissions::test_creating_a_server_returns_owner_permissions, test_admin_cannot_kick_another_admin_or_the_owner; channel_permissions::test_the_owner_sees_everything; voice_moderation::test_nobody_can_moderate_the_owner
- **PERM-16** Importing history is owner only: upload, plan, start, discard, and the mapping before the first run. Match imported authors (a danger permission, off by default) lets a member list and re-map the finished import. It is read-only for them while a newer import is pending.
  - server_import::test_only_the_owner_can_use_the_routes, test_who_may_list_and_change_the_authors, test_a_holder_lists_and_remaps_the_authors, test_a_holder_cannot_map_an_import_that_has_not_run, test_a_holder_cannot_change_the_mapping_while_its_hand_over_runs, test_a_newer_import_makes_the_old_one_read_only_for_holders, test_an_older_finished_import_is_read_only_for_holders, test_listing_the_authors_needs_a_finished_import, test_a_holder_who_loses_the_role_is_refused
- **PERM-17** Manage Channels lets a member create, edit and move channels and categories and manage forum tags. Deleting a channel or a category needs Delete Channels, without Manage Channels.
  - permissions::test_admin_manages_channels; channel_groups::test_plain_member_cannot_write_categories_or_layout; channel_crud::test_non_owner_cannot_update_or_delete; channel_permissions::test_manage_and_delete_channels_are_separate_bits
- **PERM-18** Server name, settings and icon need Manage Server, which @everyone doesn't have by default.
  - permissions::test_admin_cannot_manage_the_server_or_roles, test_channel_order_update_needs_only_manage_channels; server_settings::test_a_member_with_manage_server_manages_the_server_but_cannot_delete_it
- **PERM-19** Kick removes a member, not the owner or someone else who can kick. The kicked member is told and loses the server, their roles and their channel rules.
  - permissions::test_admin_kicks_a_member, test_owner_can_kick_an_admin; members::test_owner_kicks_member_and_everyone_is_told
- **PERM-20** Create Invite makes and manages your own invites; Manage Invites manages everyone's.
  - permissions::test_admin_manages_invites, test_creator_edits_and_revokes_own_invite_without_manage_invites, test_creator_cannot_touch_another_members_invite, test_creator_who_lost_create_invite_cannot_edit_their_invite
- **PERM-21** Manage Messages lets a member delete others' messages and moderate forum posts. It is a channel permission.
  - permissions::test_moderators_delete_other_peoples_messages_but_members_do_not; forum::test_moderators_can_edit_pin_lock_and_delete_any_post; message_edit_delete::test_a_channel_rule_grants_or_takes_manage_messages
- **PERM-22** A denied base permission holds everywhere: no View → 403 on reads and lists; no Send → a "forbidden" frame; no Connect → no voice token; Speak and Stream decide what the token may publish.
  - permissions::test_denied_view_channel_blocks_reads_and_downloads, test_denied_send_messages_is_a_forbidden_frame, test_denied_connect_blocks_the_voice_token; voice_token::test_member_without_stream_cannot_publish_screen, test_member_without_speak_and_stream_cannot_publish
- **PERM-23** Mute and Move let a member server-mute, unmute or disconnect others in voice; not themselves, not the owner, not someone with the same permission unless they are the owner.
  - voice_moderation::test_admin_can_moderate_a_member, test_only_the_owner_can_moderate_another_admin, test_moderating_yourself_is_rejected (each over mute, unmute, disconnect); fe: memberModeration (canModerateMember): no test

## Channel rules (overwrites)

- **PERM-24** A channel or category can carry a rule per role or member, over the six channel bits only (View, Send, Manage Messages, Connect, Speak, Stream). Two empty masks remove the rule. Saving the same rule changes and announces nothing.
  - channel_permissions::test_bad_bodies_are_422, test_empty_masks_delete_the_row, test_unknown_subjects_are_404, test_only_channel_bits_move; overwrite_fanout::test_a_put_that_changes_nothing_sends_no_frames_and_keeps_the_cache
- **PERM-25** In a channel: server permissions, then the category's rules, then the channel's. Within one: @everyone, roles low to high (a role without a rule uses its parent's), then the member's own rule. Without View, every channel bit is off.
  - channel_permissions::test_no_rows_change_nothing, test_everyone_deny_then_higher_role_allow, test_the_highest_role_that_says_anything_decides, test_a_role_without_a_row_inherits_its_parents_row, test_member_row_beats_roles_and_channel_beats_category, test_without_view_every_channel_bit_is_off_but_server_bits_stay; fe: channelResolution.test (scopeMask, a hand-copied mirror)
- **PERM-26** A channel or category is private when @everyone can't view it. There is no separate switch; the Private switch in the Permissions tab is the @everyone View rule.
  - overwrite_fanout::test_channel_updated_only_when_the_private_flag_flips; fe: channelResolution.test (isPrivate, privateRow); e2e: channel-permissions.spec (the switch and the @everyone row are the same setting; category settings: private on and off)
- **PERM-27** A member who can't view a channel doesn't see it anywhere, and every route by its id answers 404, so it can't be probed. A rule change applies on the next request.
  - channel_permissions::test_lists_leave_the_channel_out, test_channel_routes_are_404, test_attachments_and_viewers_of_a_hidden_channel_go_to_who_can_see_it, test_forum_routes_are_404, test_voice_routes, test_a_hidden_channel_is_404_for_its_overwrite_routes; overwrite_fanout::test_reads_right_after_a_write_see_it
- **PERM-28** They get none of its live frames, can't post in it, aren't pushed when mentioned there, and aren't offered in its @mention list.
  - channel_permissions::test_a_hidden_member_gets_none_of_the_channels_frames, test_a_hidden_member_cant_send_or_type, test_send_denied_on_one_channel; push::test_a_mention_in_a_private_channel_pushes_only_its_viewers; client mention filter: no test
- **PERM-44** The changes feed and the lookup of messages by id only list messages in channels the member can view and in their own DMs.
  - message_changes::test_changes_the_caller_cant_view_are_left_out, test_lookup_hides_what_the_caller_cant_view
- **PERM-29** Reading or writing rules needs Manage Roles and a target you can see. A non-owner writes rules only for roles and members below their rank. A manager who sees a private category only through Manage Channels can read its rules but every write is refused (403); the same channel answers 404. *(open: a manager who sees a category only through Manage Channels)*
  - channel_permissions::test_listing_shows_rows_to_role_managers_only, test_a_manager_cant_touch_roles_or_members_at_or_above_them, test_a_manager_who_sees_a_category_reads_and_writes_its_rules, test_category_rules_are_for_role_managers_who_can_see_the_category, test_a_manager_who_cant_view_a_private_category_reads_its_rules_but_cant_write_them; overwrite_fanout::test_a_put_that_changes_nothing_still_checks_permissions
- **PERM-30** A non-owner can't change a bit they lack in that channel, or save a change that takes away their own View. So a non-owner manager can make a channel private only if a lower role they may edit keeps them in. *(open: non-owner manager making a channel private)*
  - channel_permissions::test_a_manager_cant_grant_a_bit_they_dont_hold_there, test_a_manager_cant_take_away_their_own_view; e2e: channel-permissions.spec (a manager who would lose access is told before anything is sent; a manager can turn Private on while the View write for a lower role is still saving)
- **PERM-31** A channel imported as "only me" gets one @everyone-denied-View rule (and its new category too). A re-run leaves the rules alone.
  - bundle_import::test_only_me_creates_one_overwrite_hiding_the_channel_from_everyone, test_a_rerun_leaves_the_owners_edits_to_the_overwrites_alone; server_import::test_the_category_of_an_only_me_channel_is_hidden_from_members_too

## Categories

- **PERM-32** A category's rules apply to its channels unless a channel has its own. A member sees a category if they have Manage Channels, can view it, or can view one channel in it. *(open: a manager who sees a category only through one channel)*
  - channel_permissions::test_a_private_category_hides_its_channels_until_one_moves_out; overwrite_fanout::test_a_category_write_leaves_channels_with_their_own_rule_alone; channel_permissions::test_a_manager_who_cant_view_a_private_category_reads_its_rules_but_cant_write_them (the Manage Channels clause)
- **PERM-33** Moving a channel out of a private category shows it; creating one inside shows it only to viewers. Moving a channel into a private category hides it.
  - channel_permissions::test_a_private_category_hides_its_channels_until_one_moves_out, test_a_new_channel_in_a_private_category_goes_only_to_its_viewers, test_moving_a_channel_into_a_private_category_hides_it_from_who_cant_view_the_category
- **PERM-34** Deleting a category keeps its channels and removes its rules, so a formerly private category's channels become visible, and members who keep a channel are told it is public. Delete Channels is enough, even for a private category you can't see into.
  - channel_groups::test_delete_group_keeps_channels_and_messages_and_appends_them_to_ungrouped (layout only); channel_permissions::test_deleting_a_private_category_opens_its_channels_to_everyone, test_a_delete_channels_holder_can_delete_a_private_category_they_cant_see_into; channel_groups::test_deleting_a_private_category_tells_who_kept_its_channels_they_are_public

## Live updates

- **PERM-35** Members get their masks on connect (`permissions_init`, with differing channel masks of visible channels) and live (`permissions_updated`, only when their own mask changed). Role events reach every member.
  - permissions::test_permissions_init_lists_every_server_the_user_is_in, test_promote_and_demote_notify_the_target_and_the_rest, test_no_permissions_updated_when_the_mask_does_not_change; roles_api::test_only_members_whose_mask_changed_get_permissions_updated, test_role_events_are_broadcast; channel_permissions::test_permissions_init_carries_the_differing_masks_of_visible_channels; client side (serversState.canIn, setPermissions): no test
- **PERM-36** In a voice room, losing Connect removes the member; losing Speak or Stream cuts only that source.
  - roles_api::test_denying_speak_takes_the_microphone_away_mid_call, test_losing_connect_removes_the_member_from_the_call, test_changes_that_leave_voice_bits_alone_do_not_touch_the_room; overwrite_fanout::test_a_rule_that_removes_only_speak_or_stream_cuts_that_source_mid_call, test_members_in_voice_cost_no_queries
- **PERM-37** A rule change that changes what a member sees tells them: `channel_deleted` (no_access), `channel_created`, category frames, `permissions_updated`. The client then drops the channel, and moves a reader of it to the server home with "You no longer have access".
  - channel_permissions::test_losing_and_gaining_view_over_the_socket; overwrite_fanout::test_a_gained_channel_carries_its_read_state; e2e: channel-permissions.spec (making a channel private takes it away from a member without a reload)
- **PERM-38** Role managers who can see the target get `permission_overwrite_updated`, and an open Permissions tab applies it without refetching.
  - overwrite_fanout::test_overwrite_row_frame_reaches_role_managers_who_see_the_target, test_overwrite_row_frame_names_a_category_and_a_member; fe: overwritesState.test (applyFrame); e2e: channel-permissions.spec (another manager's change shows up live)
- **PERM-39** Rule frames go out after the write, in commit order. A failed fan-out keeps the write; a rejected write sends nothing. No frame names a channel the receiver can't see.
  - overwrite_fanout::test_two_quick_writes_fan_out_in_commit_order, test_a_failing_fan_out_is_logged_and_the_write_stands, test_a_rejected_write_queues_nothing, test_a_member_who_leaves_before_the_job_gets_nothing

## Matching imported authors

- **PERM-45** Every mapping change on a finished import is logged (who, author, from, to). The log outlives the import, and the last 50 entries are listed. Two changes at once log once.
  - server_import::test_a_change_is_logged_per_author, test_the_log_lists_the_newest_first_and_keeps_the_name_of_the_author, test_the_owners_draft_in_ready_is_not_logged, test_a_failed_hand_over_logs_once_and_a_retry_adds_nothing, test_a_mapped_user_who_is_gone_is_logged_as_not_mapped, test_the_log_outlives_the_import, test_a_change_that_loses_a_race_logs_and_starts_nothing, test_only_the_last_50_changes_are_listed
- **PERM-46** A hand-over tells holders its status in a narrow frame (no file name, plan, result, channel names or error text); other members get only the end.
  - server_import::test_holders_get_the_narrow_frames_of_a_hand_over_and_no_one_else, test_a_holder_gets_the_end_of_a_hand_over_resumed_after_a_restart, test_a_failed_hand_over_tells_holders_a_fixed_text_and_the_owner_the_reason

## The Permissions tab (client only)

The client copies the server's resolution (PERM-10, 11, 25) to drive the editor. What a user can actually see and do comes from the masks the server sends, so a mismatch can only make the editor wrong, not grant access.

- **PERM-40** The tab says who can see a private channel and what each toggle falls back to, and where from.
  - fe: channelResolution.test (visibleTo, inheritedForRole, inheritedForMember, opinions), overwrites.test (visibilityLine, roleInheritedText, memberInheritedText, rowSummary); e2e: channel-permissions.spec (each toggle names where its value comes from; private with nobody allowed)
- **PERM-41** A click shows at once and is marked busy until saved. Writes to one target go one at a time in click order and quick clicks collapse to the last. A failed write flips back with one toast. Logout settles pending saves silently.
  - fe: overwritesState.test (save, reset, load); e2e: channel-permissions.spec (one click sends one request; the toggle changes before a slow server answers; quick clicks on one row; a failed save flips back with one toast)
- **PERM-42** The tab disables what the manager may not change: bits they lack, roles or members at or above them, @everyone without outranking it, a change that would lose their own View.
  - e2e: channel-permissions.spec (a manager who would lose access is told before anything is sent) covers only the last; the rest: no test
- **PERM-43** Rows stay open or closed as left, and nothing overflows on a phone.
  - e2e: channel-permissions.spec (rows stay open or closed as the user left them; nothing overflows on a phone; rows open and close from the keyboard)

## Findings

**Asserted more than once** (keep the first)
- PERM-41: overwritesState.test owns the save queue. e2e "a failed save flips back with one toast" and "quick clicks on one row: the last one wins" assert the same; "the toggle changes before a slow server answers" adds only `aria-busy`. Remove the first two, fold the third into "one click sends one request".
- PERM-40: e2e "private with nobody allowed says only the owner can see it" and the text asserts in "each toggle names where its value comes from" repeat overwrites.test strings. Remove the first; move the three strings only e2e checks ("Overrides:", the inherited-role label, the member row label) to a component test.
- PERM-26: the Private switch writing the @everyone rule is asserted in five places. Keep e2e "the switch and the @everyone row are the same setting" and the category test; drop the repeats inside the other e2e tests.

**Should be one parametrised test**
- Server: the four "bits you lack" tests in roles_api cover four operations with different setups, and hidden vs Send-denied sending in channel_permissions asserts different things, so they stay separate. So do the private-flag pair and the row-frame pair in overwrite_fanout.
- fe: roles.test (resolveRole pair, memberMask Muted trio, canAssignRole, rankOf, nameColor); overwrites.test (roleInheritedText, memberInheritedText, visibilityLine); overwritesState.test (the two reset leak tests). No `it.each` exists in these files today.

**Map to no behaviour**
- e2e "rows open and close from the keyboard": tests the browser's native `<details>`; the app has no key handling there.
- overwrite_fanout's query-budget and cache tests (performance and internals; keep as such, not as behaviour); roles_migration (one-off migration).
- fe: overwrites.test's tests of trivial row helpers and the snapshot of the toggle list.
- Hand-copied twins: the frontend resolution tests copy the server's rule tests case by case, and nothing ties the two together. One shared table of cases read by both suites would replace them.

**Untested, worst regression first**
1. PERM-35 client side: `serversState.canIn` and the masks from `permissions_init` / `permissions_updated` gate the composer, voice, stream and message menus, with only Send exercised (one e2e).
2. PERM-42: the tab's guards other than losing your own View; they live inside the component.
3. PERM-23 client: which members show mute, move and kick.
4. PERM-28 client: the @mention list in a private channel.
