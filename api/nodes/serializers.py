from rest_framework import serializers as ser
from rest_framework import exceptions

from modularodm import Q
from modularodm.exceptions import ValidationValueError

from framework.auth.core import Auth
from framework.exceptions import PermissionsError

from website.models import Node, User, Comment, Institution
from website.exceptions import NodeStateError
from website.util import permissions as osf_permissions
from website.project.model import NodeUpdateError

from api.base.utils import get_user_auth, get_object_or_error, absolute_reverse
from api.base.serializers import (JSONAPISerializer, WaterbutlerLink, NodeFileHyperLinkField, IDField, TypeField,
                                  TargetTypeField, JSONAPIListField, LinksField, RelationshipField, DevOnly,
                                  HideIfRegistration, RestrictedDictSerializer,
                                  JSONAPIRelationshipSerializer, relationship_diff)
from api.base.exceptions import InvalidModelValueError, RelationshipPostMakesNoChanges


class NodeTagField(ser.Field):
    def to_representation(self, obj):
        if obj is not None:
            return obj._id
        return None

    def to_internal_value(self, data):
        return data


class NodeLicenseSerializer(RestrictedDictSerializer):

    copyright_holders = ser.ListField(allow_empty=True, read_only=True)
    year = ser.CharField(allow_blank=True, read_only=True)


class NodeSerializer(JSONAPISerializer):
    # TODO: If we have to redo this implementation in any of the other serializers, subclass ChoiceField and make it
    # handle blank choices properly. Currently DRF ChoiceFields ignore blank options, which is incorrect in this
    # instance
    filterable_fields = frozenset([
        'id',
        'title',
        'description',
        'public',
        'tags',
        'category',
        'date_created',
        'date_modified',
        'root',
        'parent',
        'contributors'
    ])

    non_anonymized_fields = [
        'id',
        'title',
        'description',
        'category',
        'date_created',
        'date_modified',
        'registration',
        'tags',
        'public',
        'license',
        'links',
        'children',
        'comments',
        'contributors',
        'files',
        'node_links',
        'parent',
        'root',
        'logs',
        'wikis'
    ]

    id = IDField(source='_id', read_only=True)
    type = TypeField()

    category_choices = Node.CATEGORY_MAP.items()
    category_choices_string = ', '.join(["'{}'".format(choice[0]) for choice in category_choices])

    title = ser.CharField(required=True)
    description = ser.CharField(required=False, allow_blank=True, allow_null=True)
    category = ser.ChoiceField(choices=category_choices, help_text="Choices: " + category_choices_string)
    date_created = ser.DateTimeField(read_only=True)
    date_modified = ser.DateTimeField(read_only=True)
    registration = ser.BooleanField(read_only=True, source='is_registration')
    fork = ser.BooleanField(read_only=True, source='is_fork')
    collection = ser.BooleanField(read_only=True, source='is_collection')
    tags = JSONAPIListField(child=NodeTagField(), required=False)
    node_license = NodeLicenseSerializer(read_only=True, required=False)
    template_from = ser.CharField(required=False, allow_blank=False, allow_null=False,
                                  help_text='Specify a node id for a node you would like to use as a template for the '
                                            'new node. Templating is like forking, except that you do not copy the '
                                            'files, only the project structure. Some information is changed on the top '
                                            'level project by submitting the appropriate fields in the request body, '
                                            'and some information will not change. By default, the description will '
                                            'be cleared and the project will be made private.')
    current_user_permissions = ser.SerializerMethodField(help_text='List of strings representing the permissions '
                                                                   'for the current user on this node.')

    # Public is only write-able by admins--see update method
    public = ser.BooleanField(source='is_public', required=False,
                              help_text='Nodes that are made public will give read-only access '
                                        'to everyone. Private nodes require explicit read '
                                        'permission. Write and admin access are the same for '
                                        'public and private nodes. Administrators on a parent '
                                        'node have implicit read permissions for all child nodes')

    links = LinksField({'html': 'get_absolute_html_url'})
    # TODO: When we have osf_permissions.ADMIN permissions, make this writable for admins

    license = RelationshipField(
        related_view='licenses:license-detail',
        related_view_kwargs={'license_id': '<node_license.node_license._id>'},
    )

    children = RelationshipField(
        related_view='nodes:node-children',
        related_view_kwargs={'node_id': '<pk>'},
        related_meta={'count': 'get_node_count'},
    )

    comments = RelationshipField(
        related_view='nodes:node-comments',
        related_view_kwargs={'node_id': '<pk>'},
        related_meta={'unread': 'get_unread_comments_count'})

    contributors = RelationshipField(
        related_view='nodes:node-contributors',
        related_view_kwargs={'node_id': '<pk>'},
        related_meta={'count': 'get_contrib_count'},
    )

    files = RelationshipField(
        related_view='nodes:node-providers',
        related_view_kwargs={'node_id': '<pk>'}
    )

    wikis = RelationshipField(
        related_view='nodes:node-wikis',
        related_view_kwargs={'node_id': '<pk>'}
    )

    forked_from = RelationshipField(
        related_view=lambda n: 'registrations:registration-detail' if getattr(n, 'is_registration', False) else 'nodes:node-detail',
        related_view_kwargs={'node_id': '<forked_from_id>'}
    )

    forks = RelationshipField(
        related_view='nodes:node-forks',
        related_view_kwargs={'node_id': '<pk>'}
    )

    node_links = RelationshipField(
        related_view='nodes:node-pointers',
        related_view_kwargs={'node_id': '<pk>'},
        related_meta={'count': 'get_pointers_count'},
    )

    parent = RelationshipField(
        related_view='nodes:node-detail',
        related_view_kwargs={'node_id': '<parent_node._id>'},
        filter_key='parent_node'
    )

    registrations = DevOnly(HideIfRegistration(RelationshipField(
        related_view='nodes:node-registrations',
        related_view_kwargs={'node_id': '<pk>'},
        related_meta={'count': 'get_registration_count'}
    )))

    affiliated_institutions = RelationshipField(
        related_view='nodes:node-institutions',
        related_view_kwargs={'node_id': '<pk>'},
        self_view='nodes:node-relationships-institutions',
        self_view_kwargs={'node_id': '<pk>'}
    )

    root = RelationshipField(
        related_view='nodes:node-detail',
        related_view_kwargs={'node_id': '<root._id>'}
    )

    logs = RelationshipField(
        related_view='nodes:node-logs',
        related_view_kwargs={'node_id': '<pk>'},
        related_meta={'count': 'get_logs_count'}
    )

    def get_current_user_permissions(self, obj):
        user = self.context['request'].user
        if user.is_anonymous():
            return ['read']
        permissions = obj.get_permissions(user=user)
        if not permissions:
            permissions = ['read']
        return permissions

    class Meta:
        type_ = 'nodes'

    def get_absolute_url(self, obj):
        return obj.get_absolute_url()

    # TODO: See if we can get the count filters into the filter rather than the serializer.

    def get_logs_count(self, obj):
        return len(obj.logs)

    def get_node_count(self, obj):
        auth = get_user_auth(self.context['request'])
        nodes = [node for node in obj.nodes if node.can_view(auth) and node.primary and not node.is_deleted]
        return len(nodes)

    def get_contrib_count(self, obj):
        return len(obj.contributors)

    def get_registration_count(self, obj):
        auth = get_user_auth(self.context['request'])
        registrations = [node for node in obj.registrations_all if node.can_view(auth)]
        return len(registrations)

    def get_pointers_count(self, obj):
        return len(obj.nodes_pointer)

    def get_unread_comments_count(self, obj):
        user = get_user_auth(self.context['request']).user
        node_comments = Comment.find_n_unread(user=user, node=obj, page='node')

        return {
            'node': node_comments
        }

    def create(self, validated_data):
        if 'template_from' in validated_data:
            request = self.context['request']
            user = request.user
            template_from = validated_data.pop('template_from')
            template_node = Node.load(key=template_from)
            if template_node is None:
                raise exceptions.NotFound
            if not template_node.has_permission(user, 'read', check_parent=False):
                raise exceptions.PermissionDenied

            validated_data.pop('creator')
            changed_data = {template_from: validated_data}
            node = template_node.use_as_template(auth=get_user_auth(request), changes=changed_data)
        else:
            node = Node(**validated_data)
        try:
            node.save()
        except ValidationValueError as e:
            raise InvalidModelValueError(detail=e.message)
        return node

    def update(self, node, validated_data):
        """Update instance with the validated data. Requires
        the request to be in the serializer context.
        """
        assert isinstance(node, Node), 'node must be a Node'
        auth = get_user_auth(self.context['request'])
        old_tags = set([tag._id for tag in node.tags])
        if 'tags' in validated_data:
            current_tags = set(validated_data.pop('tags', []))
        elif self.partial:
            current_tags = set(old_tags)
        else:
            current_tags = set()

        for new_tag in (current_tags - old_tags):
            node.add_tag(new_tag, auth=auth)
        for deleted_tag in (old_tags - current_tags):
            node.remove_tag(deleted_tag, auth=auth)

        if validated_data:
            try:
                node.update(validated_data, auth=auth)
            except ValidationValueError as e:
                raise InvalidModelValueError(detail=e.message)
            except PermissionsError:
                raise exceptions.PermissionDenied
            except NodeUpdateError as e:
                raise exceptions.ValidationError(detail=e.reason)
            except NodeStateError as e:
                raise InvalidModelValueError(detail=e.message)

        return node


class NodeDetailSerializer(NodeSerializer):
    """
    Overrides NodeSerializer to make id required.
    """
    id = IDField(source='_id', required=True)


class NodeForksSerializer(NodeSerializer):

    category_choices = Node.CATEGORY_MAP.items()
    category_choices_string = ', '.join(["'{}'".format(choice[0]) for choice in category_choices])

    title = ser.CharField(required=False)
    category = ser.ChoiceField(read_only=True, choices=category_choices, help_text="Choices: " + category_choices_string)
    forked_date = ser.DateTimeField(read_only=True)

    def create(self, validated_data):
        node = validated_data.pop('node')
        fork_title = validated_data.pop('title', 'Fork of ')
        request = self.context['request']
        auth = get_user_auth(request)
        fork = node.fork_node(auth, title=fork_title)

        try:
            fork.save()
        except ValidationValueError as e:
            raise InvalidModelValueError(detail=e.message)

        return fork


class NodeContributorsSerializer(JSONAPISerializer):
    """ Separate from UserSerializer due to necessity to override almost every field as read only
    """
    non_anonymized_fields = ['bibliographic', 'permission']
    filterable_fields = frozenset([
        'id',
        'bibliographic',
        'permission'
    ])

    id = IDField(source='_id', required=True)
    type = TypeField()

    bibliographic = ser.BooleanField(help_text='Whether the user will be included in citations for this node or not.',
                                     default=True)
    permission = ser.ChoiceField(choices=osf_permissions.PERMISSIONS, required=False, allow_null=True,
                                 default=osf_permissions.reduce_permissions(osf_permissions.DEFAULT_CONTRIBUTOR_PERMISSIONS),
                                 help_text='User permission level. Must be "read", "write", or "admin". Defaults to "write".')
    unregistered_contributor = ser.SerializerMethodField()

    links = LinksField({
        'self': 'get_absolute_url'
    })

    users = RelationshipField(
        related_view='users:user-detail',
        related_view_kwargs={'user_id': '<pk>'},
        always_embed=True
    )

    class Meta:
        type_ = 'contributors'

    def get_absolute_url(self, obj):
        node_id = self.context['request'].parser_context['kwargs']['node_id']
        return absolute_reverse(
            'nodes:node-contributor-detail',
            kwargs={
                'node_id': node_id,
                'user_id': obj._id
            }
        )

    def get_unregistered_contributor(self, obj):
        unclaimed_records = obj.unclaimed_records.get(obj.node_id, None)
        if unclaimed_records:
            return unclaimed_records.get('name', None)

class NodeContributorsCreateSerializer(NodeContributorsSerializer):
    """
    Overrides NodeContributorsSerializer to add target_type field
    """
    target_type = TargetTypeField(target_type='users')

    def create(self, validated_data):
        auth = Auth(self.context['request'].user)
        node = self.context['view'].get_node()
        contributor = get_object_or_error(User, validated_data['_id'], display_name='user')
        # Node object checks for contributor existence but can still change permissions anyway
        if contributor in node.contributors:
            raise exceptions.ValidationError('{} is already a contributor'.format(contributor.fullname))

        bibliographic = validated_data['bibliographic']
        permissions = osf_permissions.expand_permissions(validated_data.get('permission')) or osf_permissions.DEFAULT_CONTRIBUTOR_PERMISSIONS
        node.add_contributor(contributor=contributor, auth=auth, visible=bibliographic, permissions=permissions, save=True)
        contributor.permission = osf_permissions.reduce_permissions(node.get_permissions(contributor))
        contributor.bibliographic = node.get_visible(contributor)
        contributor.node_id = node._id
        return contributor


class NodeContributorDetailSerializer(NodeContributorsSerializer):
    """
    Overrides node contributor serializer to add additional methods
    """

    def update(self, instance, validated_data):
        contributor = instance
        auth = Auth(self.context['request'].user)
        node = self.context['view'].get_node()

        visible = validated_data.get('bibliographic')
        permission = validated_data.get('permission')
        try:
            node.update_contributor(contributor, permission, visible, auth, save=True)
        except NodeStateError as e:
            raise exceptions.ValidationError(detail=e.message)
        except ValueError as e:
            raise exceptions.ValidationError(detail=e.message)
        contributor.permission = osf_permissions.reduce_permissions(node.get_permissions(contributor))
        contributor.bibliographic = node.get_visible(contributor)
        contributor.node_id = node._id
        return contributor


class NodeLinksSerializer(JSONAPISerializer):

    id = IDField(source='_id')
    type = TypeField()
    target_type = TargetTypeField(target_type='nodes')

    # TODO: We don't show the title because the current user may not have access to this node. We may want to conditionally
    # include this field in the future.
    # title = ser.CharField(read_only=True, source='node.title', help_text='The title of the node that this Node Link '
    #                                                                      'points to')

    target_node = RelationshipField(
        related_view='nodes:node-detail',
        related_view_kwargs={'node_id': '<pk>'},
        always_embed=True

    )
    class Meta:
        type_ = 'node_links'

    links = LinksField({
        'self': 'get_absolute_url'
    })

    def get_absolute_url(self, obj):
        node_id = self.context['request'].parser_context['kwargs']['node_id']
        return absolute_reverse(
            'nodes:node-pointer-detail',
            kwargs={
                'node_id': node_id,
                'node_link_id': obj._id
            }
        )

    def create(self, validated_data):
        request = self.context['request']
        user = request.user
        auth = Auth(user)
        node = self.context['view'].get_node()
        target_node_id = validated_data['_id']
        pointer_node = Node.load(target_node_id)
        if not pointer_node or pointer_node.is_collection:
            raise InvalidModelValueError(
                source={'pointer': '/data/relationships/node_links/data/id'},
                detail='Target Node \'{}\' not found.'.format(target_node_id)
            )
        try:
            pointer = node.add_pointer(pointer_node, auth, save=True)
            return pointer
        except ValueError:
            raise InvalidModelValueError(
                source={'pointer': '/data/relationships/node_links/data/id'},
                detail='Target Node \'{}\' already pointed to by \'{}\'.'.format(target_node_id, node._id)
            )

    def update(self, instance, validated_data):
        pass


class NodeProviderSerializer(JSONAPISerializer):
    id = ser.SerializerMethodField(read_only=True)
    kind = ser.CharField(read_only=True)
    name = ser.CharField(read_only=True)
    path = ser.CharField(read_only=True)
    node = ser.CharField(source='node_id', read_only=True)
    provider = ser.CharField(read_only=True)
    files = NodeFileHyperLinkField(
        related_view='nodes:node-files',
        related_view_kwargs={'node_id': '<node_id>', 'path': '<path>', 'provider': '<provider>'},
        kind='folder',
        never_embed=True
    )
    links = LinksField({
        'upload': WaterbutlerLink(),
        'new_folder': WaterbutlerLink(kind='folder')
    })

    class Meta:
        type_ = 'files'

    @staticmethod
    def get_id(obj):
        return '{}:{}'.format(obj.node._id, obj.provider)

    def get_absolute_url(self, obj):
        return absolute_reverse(
            'nodes:node-provider-detail',
            kwargs={
                'node_id': obj.node._id,
                'provider': obj.provider
            }
        )

class InstitutionRelated(JSONAPIRelationshipSerializer):
    id = ser.CharField(source='_id', required=False, allow_null=True)
    class Meta:
        type_ = 'institutions'

class NodeInstitutionsRelationshipSerializer(ser.Serializer):
    data = ser.ListField(child=InstitutionRelated())
    links = LinksField({'self': 'get_self_url',
                        'html': 'get_related_url'})

    def get_self_url(self, obj):
        return obj['self'].institutions_relationship_url()

    def get_related_url(self, obj):
        return obj['self'].institutions_url()

    class Meta:
        type_ = 'institutions'

    def get_institutions_to_add_remove(self, institutions, new_institutions):
        diff = relationship_diff(
            current_items={inst._id: inst for inst in institutions},
            new_items={inst['_id']: inst for inst in new_institutions}
        )

        insts_to_add = []
        for inst_id in diff['add']:
            inst = Institution.load(inst_id)
            if not inst:
                raise exceptions.NotFound(detail='Institution with id "{}" was not found'.format(inst_id))
            insts_to_add.append(inst)

        return insts_to_add, diff['remove'].values()

    def make_instance_obj(self, obj):
        return {
            'data': obj.affiliated_institutions,
            'self': obj
        }

    def update(self, instance, validated_data):
        node = instance['self']
        user = self.context['request'].user

        add, remove = self.get_institutions_to_add_remove(
            institutions=instance['data'],
            new_institutions=validated_data['data']
        )

        for inst in add:
            if inst not in user.affiliated_institutions:
                raise exceptions.PermissionDenied(detail='User needs to be affiliated with {}'.format(inst.name))

        for inst in remove:
            node.remove_affiliated_institution(inst, user)
        for inst in add:
            node.add_affiliated_institution(inst, user)
        node.save()

        return self.make_instance_obj(node)

    def create(self, validated_data):
        instance = self.context['view'].get_object()
        user = self.context['request'].user
        node = instance['self']

        add, remove = self.get_institutions_to_add_remove(
            institutions=instance['data'],
            new_institutions=validated_data['data']
        )
        if not len(add):
            raise RelationshipPostMakesNoChanges

        for inst in add:
            if inst not in user.affiliated_institutions:
                raise exceptions.PermissionDenied(detail='User needs to be affiliated with {}'.format(inst.name))

        for inst in add:
            node.add_affiliated_institution(inst, user)
        node.save()

        return self.make_instance_obj(node)


class NodeAlternativeCitationSerializer(JSONAPISerializer):

    id = IDField(source="_id", read_only=True)
    type = TypeField()
    name = ser.CharField(required=True)
    text = ser.CharField(required=True)

    class Meta:
        type_ = 'citations'

    def create(self, validated_data):
        errors = self.error_checker(validated_data)
        if len(errors) > 0:
            raise exceptions.ValidationError(detail=errors)
        node = self.context['view'].get_node()
        auth = Auth(self.context['request']._user)
        citation = node.add_citation(auth, save=True, **validated_data)
        return citation

    def update(self, instance, validated_data):
        errors = self.error_checker(validated_data)
        if len(errors) > 0:
            raise exceptions.ValidationError(detail=errors)
        node = self.context['view'].get_node()
        auth = Auth(self.context['request']._user)
        instance = node.edit_citation(auth, instance, save=True, **validated_data)
        return instance

    def error_checker(self, data):
        errors = []
        name = data.get('name', None)
        text = data.get('text', None)
        citations = self.context['view'].get_node().alternative_citations
        if not (self.instance and self.instance.name == name) and citations.find(Q('name', 'eq', name)).count() > 0:
            errors.append("There is already a citation named '{}'".format(name))
        if not (self.instance and self.instance.text == text):
            matching_citations = citations.find(Q('text', 'eq', text))
            if matching_citations.count() > 0:
                names = "', '".join([str(citation.name) for citation in matching_citations])
                errors.append("Citation matches '{}'".format(names))
        return errors

    def get_absolute_url(self, obj):
        #  Citations don't have urls
        raise NotImplementedError
